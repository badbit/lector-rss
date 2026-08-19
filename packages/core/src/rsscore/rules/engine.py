"""Evaluación de reglas.

Dos decisiones que condicionan todo el módulo:

* **Regex precompiladas una sola vez.** Las reglas se evalúan contra cientos de
  miles de entradas; recompilar el patrón en cada comparación es el error clásico
  de rendimiento aquí.
* **Comparación insensible a acentos.** Es un lector en español: quien escribe la
  regla «energia» espera encontrar «Energía». Se normaliza con NFKD y se
  descartan los diacríticos, salvo que la condición pida `case_sensitive`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from ..models import Entry, Feed
from .models import Action, Condition, ConditionGroup, Op, Rule, RuleField, Scope

__all__ = ["RuleEngine", "fold"]


def fold(text: str | None) -> str:
    """Normaliza para comparar: sin acentos, sin mayúsculas, sin espacios de más."""
    if not text:
        return ""
    descompuesto = unicodedata.normalize("NFKD", text)
    sin_tildes = "".join(c for c in descompuesto if not unicodedata.combining(c))
    return sin_tildes.casefold().strip()


class _Contexto:
    """Campos del artículo, calculados y normalizados una sola vez.

    Sin esto se volvía a plegar el cuerpo entero en cada condición de cada regla:
    con 50 reglas eso son 100 normalizaciones del mismo texto por artículo, y era
    el coste dominante de toda la evaluación.
    """

    __slots__ = ("_crudo", "_plegado", "entry", "feed")

    def __init__(self, entry: Entry, feed: Feed) -> None:
        self.entry = entry
        self.feed = feed
        self._crudo: dict[RuleField, str] = {}
        self._plegado: dict[RuleField, str] = {}

    def crudo(self, field: RuleField) -> str:
        valor = self._crudo.get(field)
        if valor is None:
            valor = _campo(field, self.entry, self.feed)
            self._crudo[field] = valor
        return valor

    def plegado(self, field: RuleField) -> str:
        valor = self._plegado.get(field)
        if valor is None:
            valor = fold(self.crudo(field))
            self._plegado[field] = valor
        return valor


def _campo(field: RuleField, entry: Entry, feed: Feed) -> str:
    match field:
        case RuleField.TITLE:
            return entry.title or ""
        case RuleField.CONTENT:
            return entry.body_text or entry.summary or ""
        case RuleField.SUMMARY:
            return entry.summary or ""
        case RuleField.AUTHOR:
            return entry.author or ""
        case RuleField.URL:
            return entry.url or ""
        case RuleField.FEED_TITLE:
            return feed.display_title
        case RuleField.ANY:
            return " \n".join(
                p for p in (entry.title, entry.body_text or entry.summary, entry.author) if p
            )
    return ""


class RuleEngine:
    """Evalúa un conjunto de reglas contra artículos."""

    def __init__(self, rules: Iterable[Rule]) -> None:
        self.rules: list[Rule] = sorted(
            (r for r in rules if r.enabled), key=lambda r: (r.position, r.name)
        )
        self._regex: dict[tuple[str, bool], re.Pattern[str]] = {}
        self._scope_cache: dict[str, frozenset[str]] = {}
        self._precompile()

    # ------------------------------------------------------------- compilación
    def _precompile(self) -> None:
        for rule in self.rules:
            self._walk_group(rule.when)

    def _walk_group(self, group: ConditionGroup) -> None:
        for item in (*group.all, *group.any, *group.none):
            if isinstance(item, ConditionGroup):
                self._walk_group(item)
            elif item.op in (Op.MATCHES, Op.NOT_MATCHES):
                self._pattern(str(item.value), item.case_sensitive)

    def _pattern(self, value: str, case_sensitive: bool) -> re.Pattern[str]:
        key = (value, case_sensitive)
        pat = self._regex.get(key)
        if pat is None:
            flags = 0 if case_sensitive else re.IGNORECASE
            pat = re.compile(value, flags)
            self._regex[key] = pat
        return pat

    # -------------------------------------------------------------- evaluación
    def evaluate(
        self,
        entry: Entry,
        feed: Feed,
        *,
        folder_names: Iterable[str] = (),
        tag_names: Iterable[str] = (),
    ) -> list[Action]:
        """Devuelve las acciones a ejecutar, en orden y respetando `stop`."""
        folders = frozenset(fold(n) for n in folder_names)
        tags = frozenset(fold(n) for n in tag_names)
        ctx = _Contexto(entry, feed)
        acciones: list[Action] = []
        for rule in self.rules:
            if not self._in_scope(rule.scope, feed, folders, tags):
                continue
            if not self._grupo(rule.when, ctx):
                continue
            for action in rule.then:
                if action.stop:
                    return acciones
                acciones.append(action)
        return acciones

    def matching_rules(
        self,
        entry: Entry,
        feed: Feed,
        *,
        folder_names: Iterable[str] = (),
        tag_names: Iterable[str] = (),
    ) -> list[tuple[Rule, list[Action]]]:
        """Como `evaluate`, pero diciendo qué regla aportó cada acción.

        Lo necesita la capa de aplicación para poder decir en la notificación
        «40 artículos coinciden con «Alertas Rust»».
        """
        folders = frozenset(fold(n) for n in folder_names)
        tags = frozenset(fold(n) for n in tag_names)
        ctx = _Contexto(entry, feed)
        salida: list[tuple[Rule, list[Action]]] = []
        for rule in self.rules:
            if not self._in_scope(rule.scope, feed, folders, tags):
                continue
            if not self._grupo(rule.when, ctx):
                continue
            acciones: list[Action] = []
            for action in rule.then:
                if action.stop:
                    if acciones:
                        salida.append((rule, acciones))
                    return salida
                acciones.append(action)
            if acciones:
                salida.append((rule, acciones))
        return salida

    def evaluate_group(self, group: ConditionGroup, entry: Entry, feed: Feed) -> bool:
        """Un grupo vacío se cumple siempre: la regla cubre todo su ámbito."""
        return self._grupo(group, _Contexto(entry, feed))

    def _grupo(self, group: ConditionGroup, ctx: _Contexto) -> bool:
        if group.is_empty():
            return True
        if group.all and not all(self._eval(i, ctx) for i in group.all):
            return False
        if group.any and not any(self._eval(i, ctx) for i in group.any):
            return False
        return not (group.none and any(self._eval(i, ctx) for i in group.none))

    def _eval(self, item: Condition | ConditionGroup, ctx: _Contexto) -> bool:
        if isinstance(item, ConditionGroup):
            return self._grupo(item, ctx)
        return self._condicion(item, ctx)

    # --------------------------------------------------------------- ámbito
    def _in_scope(
        self, scope: Scope, feed: Feed, folders: frozenset[str], tags: frozenset[str]
    ) -> bool:
        if scope.is_empty():
            return True
        # Acepta ids o nombres indistintamente: el usuario escribe la regla a mano.
        for ref in scope.feeds:
            if ref == feed.id or fold(ref) in {
                fold(feed.title),
                fold(feed.custom_title),
                fold(feed.url),
            }:
                return True
        for ref in scope.folders:
            if ref == feed.folder_id or fold(ref) in folders:
                return True
        return any(fold(ref) in tags for ref in scope.tags)

    # ------------------------------------------------------------ condiciones
    def matches(self, condition: Condition, entry: Entry, feed: Feed) -> bool:
        """Evalúa una condición suelta (lo usan las pruebas y la vista previa)."""
        return self._condicion(condition, _Contexto(entry, feed))

    def _condicion(self, condition: Condition, ctx: _Contexto) -> bool:
        valor = str(condition.value)

        if condition.op in (Op.MATCHES, Op.NOT_MATCHES):
            # La regex se aplica al texto original: el usuario puede querer \b,
            # mayúsculas o acentos explícitos y debemos respetarlos.
            texto = ctx.crudo(condition.field)
            hit = bool(self._pattern(valor, condition.case_sensitive).search(texto))
            return hit if condition.op is Op.MATCHES else not hit

        if condition.op in (Op.GT, Op.LT):
            return self._compare_numeric(condition, ctx.crudo(condition.field))

        if condition.case_sensitive:
            izq, der = ctx.crudo(condition.field), valor
        else:
            izq, der = ctx.plegado(condition.field), fold(valor)

        match condition.op:
            case Op.CONTAINS:
                return der in izq
            case Op.NOT_CONTAINS:
                return der not in izq
            case Op.EQUALS:
                return izq == der
            case Op.STARTS_WITH:
                return izq.startswith(der)
            case Op.ENDS_WITH:
                return izq.endswith(der)
        return False

    @staticmethod
    def _compare_numeric(condition: Condition, texto: str) -> bool:
        """`gt`/`lt` comparan números; sobre texto comparan su longitud.

        Sirve para reglas del tipo «descarta los teletipos de menos de 200
        caracteres», que es su uso real.
        """
        try:
            derecha = float(condition.value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        try:
            izquierda = float(texto.strip())
        except ValueError:
            izquierda = float(len(texto))
        return izquierda > derecha if condition.op is Op.GT else izquierda < derecha

    @staticmethod
    def _field_value(field: RuleField, entry: Entry, feed: Feed) -> str:
        return _campo(field, entry, feed)
