"""Esquema declarativo de las reglas, validado con Pydantic v2.

El formato que ve el usuario (y que puede versionar en un fichero YAML) es:

```yaml
- name: Alertas Rust
  enabled: true
  when:
    any:
      - { field: title,   op: matches,  value: '(?i)\\brust\\b' }
      - { field: content, op: contains, value: cargo }
  scope: { folders: [Dev] }
  then:
    - { tag: rust }
    - { star: true }
    - { notify: { priority: high } }
```

Todo lo que pueda fallar se valida aquí y no durante la ingesta: en particular
las expresiones regulares se compilan en el validador, para que una regex mal
escrita se rechace al guardar la regla con un mensaje legible en lugar de
reventar en mitad de un refresco con 500 feeds en vuelo.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..ids import new_id
from ..models import ExportKind
from ..notify import Priority


class RuleField(StrEnum):
    """Sobre qué parte del artículo se evalúa la condición."""

    TITLE = "title"
    CONTENT = "content"  # cuerpo completo, o el resumen si no hay cuerpo
    SUMMARY = "summary"
    AUTHOR = "author"
    URL = "url"
    FEED_TITLE = "feed_title"
    ANY = "any"  # título + contenido + autor


class Op(StrEnum):
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    MATCHES = "matches"
    NOT_MATCHES = "not_matches"
    EQUALS = "equals"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    GT = "gt"
    LT = "lt"


REGEX_OPS = frozenset({Op.MATCHES, Op.NOT_MATCHES})


class Condition(BaseModel):
    """Una comparación elemental sobre un campo del artículo."""

    model_config = ConfigDict(extra="forbid")

    field: RuleField = RuleField.ANY
    op: Op = Op.CONTAINS
    value: str | int | float | bool = ""
    case_sensitive: bool = False

    @model_validator(mode="after")
    def _compile_regex(self) -> Condition:
        """Compila la regex ya, para que una regex inválida no llegue a la ingesta."""
        if self.op in REGEX_OPS:
            pattern = str(self.value)
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"expresión regular inválida en la condición "
                    f"{self.field}/{self.op} → {pattern!r}: {exc}"
                ) from exc
        return self


class ConditionGroup(BaseModel):
    """Combinación booleana de condiciones, anidable sin límite.

    * `all`  → todas deben cumplirse
    * `any`  → basta con una
    * `none` → ninguna debe cumplirse

    Un grupo vacío se cumple siempre (la regla se aplica a todo su ámbito).
    """

    model_config = ConfigDict(extra="forbid")

    all: list[Condition | ConditionGroup] = Field(default_factory=list)
    any: list[Condition | ConditionGroup] = Field(default_factory=list)
    none: list[Condition | ConditionGroup] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.all or self.any or self.none)


ConditionGroup.model_rebuild()


class Scope(BaseModel):
    """Dónde se aplica la regla. Acepta nombres o ids; vacío = todo el archivo.

    Si se rellena más de una lista el ámbito es la UNIÓN: `feeds` + `folders` +
    `tags` describen orígenes, no filtros que se intersecan.
    """

    model_config = ConfigDict(extra="forbid")

    feeds: list[str] = Field(default_factory=list)
    folders: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.feeds or self.folders or self.tags)


class NotifySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority: Priority = Priority.DEFAULT
    title: str | None = None


class ExportSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ExportKind = ExportKind.OBSIDIAN
    target: str = "desktop"

    @field_validator("kind")
    @classmethod
    def _kind_por_articulo(cls, v: ExportKind) -> ExportKind:
        if v is ExportKind.MAGAZINE:
            raise ValueError(
                "la exportación 'magazine' agrupa varios artículos y no puede ser "
                "acción de una regla; usa 'obsidian' o 'kindle'"
            )
        return v


class ActionKind(StrEnum):
    TAG = "tag"
    UNTAG = "untag"
    STAR = "star"
    MARK_READ = "mark_read"
    NOTIFY = "notify"
    EXPORT = "export"
    STOP = "stop"


class Action(BaseModel):
    """Una acción. En YAML es un mapa de una sola clave: `{ tag: rust }`."""

    model_config = ConfigDict(extra="forbid")

    tag: str | None = None
    untag: str | None = None
    star: bool | None = None
    mark_read: bool | None = None
    notify: NotifySpec | None = None
    export: ExportSpec | None = None
    stop: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _atajos(cls, data: Any) -> Any:
        """Admite `{notify: true}`, `{export: obsidian}` y `stop` a secas."""
        if isinstance(data, str):
            return {data: True}
        if not isinstance(data, dict):
            return data
        data = dict(data)
        # Un dict con TODAS las claves de acción presentes viene de `model_dump`,
        # no del YAML del usuario: ahí `notify: null` significa "sin notify", y
        # aplicarle los atajos rompería el ida y vuelta al guardar la regla.
        claves = {str(k) for k in ActionKind}
        es_dump = claves.issubset(data.keys())
        if not es_dump and ("notify" in data and data["notify"] in (True, None)):
            data["notify"] = {}
        if isinstance(data.get("export"), str):
            data["export"] = {"kind": data["export"]}
        return data

    @model_validator(mode="after")
    def _una_sola(self) -> Action:
        puestas = [k for k in ActionKind if getattr(self, str(k)) is not None]
        if len(puestas) != 1:
            raise ValueError(
                "cada acción debe indicar exactamente una operación "
                f"({', '.join(str(k) for k in ActionKind)}); se recibieron: "
                f"{', '.join(str(k) for k in puestas) or 'ninguna'}"
            )
        return self

    @property
    def kind(self) -> ActionKind:
        for k in ActionKind:
            if getattr(self, str(k)) is not None:
                return k
        raise ValueError("acción vacía")  # pragma: no cover - lo impide el validador

    def to_yaml_obj(self) -> dict[str, Any]:
        """Forma compacta para exportar: sólo la clave que está puesta."""
        k = self.kind
        value = getattr(self, str(k))
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json", exclude_none=True)
        return {str(k): value}


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    name: str
    enabled: bool = True
    position: int = 0
    when: ConditionGroup = Field(default_factory=ConditionGroup)
    scope: Scope = Field(default_factory=Scope)
    then: list[Action] = Field(default_factory=list)

    def to_yaml_obj(self) -> dict[str, Any]:
        """Representación limpia para `export_rules_yaml` (sin ruido de defaults)."""
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "position": self.position,
        }
        when = self.when.model_dump(mode="json", exclude_defaults=True)
        if when:
            out["when"] = when
        scope = self.scope.model_dump(mode="json", exclude_defaults=True)
        if scope:
            out["scope"] = scope
        out["then"] = [a.to_yaml_obj() for a in self.then]
        return out


def parse_rules(data: Any) -> list[Rule]:
    """Valida una lista de reglas (o un `{rules: [...]}`) venida de YAML/JSON."""
    if data is None:
        return []
    if isinstance(data, dict):
        data = data.get("rules", [])
    if not isinstance(data, list):
        raise ValueError("el fichero de reglas debe ser una lista de reglas")
    reglas: list[Rule] = []
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            raise ValueError(f"la regla #{i + 1} no es un mapa")
        rule = Rule.model_validate(raw)
        if "position" not in raw:
            rule.position = i  # el orden del fichero es el orden de aplicación
        reglas.append(rule)
    return reglas
