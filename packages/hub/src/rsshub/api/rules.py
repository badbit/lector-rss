"""Reglas de filtrado, alertas y carpetas inteligentes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from rsscore import repo
from rsscore.models import EntrySelection

from ..deps import bus, config, db, require_token, write_tx

router = APIRouter(prefix="/rules", tags=["rules"], dependencies=[Depends(require_token)])


@router.get("")
def list_rules() -> list[dict]:
    from rsscore.rules.store import load_rules

    return [r.model_dump() for r in load_rules(db())]


@router.put("/{rule_id}")
def put_rule(rule_id: str, body: dict) -> dict:
    from rsscore.rules.models import Rule
    from rsscore.rules.store import save_rule

    body["id"] = rule_id
    try:
        rule = Rule.model_validate(body)
    except Exception as exc:
        raise HTTPException(422, f"Regla inválida: {exc}") from exc
    with write_tx() as conn:
        save_rule(conn, rule)
    bus.publish({"type": "rules_changed"})
    return rule.model_dump()


@router.delete("/{rule_id}", status_code=204)
def delete_rule(rule_id: str) -> Response:
    from rsscore.rules.store import delete_rule as do_delete

    with write_tx() as conn:
        do_delete(conn, rule_id)
    bus.publish({"type": "rules_changed"})
    return Response(status_code=204)


@router.get("/export", response_class=Response)
def export_yaml() -> Response:
    from rsscore.rules.store import export_rules_yaml

    return Response(content=export_rules_yaml(db()), media_type="text/yaml")


@router.post("/import")
async def import_yaml(request: Request) -> dict:
    from rsscore.rules.store import import_rules_yaml

    cuerpo = await request.body()
    with write_tx() as conn:
        n = import_rules_yaml(conn, cuerpo.decode("utf-8"))
    bus.publish({"type": "rules_changed"})
    return {"importadas": n}


class BackfillRequest(BaseModel):
    rule_id: str | None = None
    limit: int = 5000


@router.post("/backfill")
def backfill(req: BackfillRequest) -> dict:
    """Aplica las reglas al archivo ya descargado.

    Al crear una regla nueva uno espera que actúe también sobre lo que ya está
    guardado, no solo sobre lo que llegue a partir de ahora.
    """
    from rsscore.rules.apply import apply_rules
    from rsscore.rules.engine import RuleEngine
    from rsscore.rules.store import load_rules

    conn = db()
    rules = load_rules(conn)
    if req.rule_id:
        rules = [r for r in rules if r.id == req.rule_id]
    if not rules:
        raise HTTPException(404, "No hay reglas que aplicar")
    engine = RuleEngine(rules)
    entries = repo.select_entries(conn, EntrySelection(limit=req.limit))
    aplicadas = 0
    with write_tx() as c:
        for entry in entries:
            feed = repo.get_feed(c, entry.feed_id)
            if not feed:
                continue
            entry.body_html, entry.body_text = repo.get_body(c, entry.id)
            outcome = apply_rules(c, entry, feed, engine)
            if getattr(outcome, "applied_rules", None):
                aplicadas += 1
    bus.publish({"type": "state_changed"})
    return {"revisadas": len(entries), "afectadas": aplicadas}


# ---------------------------------------------------------- carpetas inteligentes
smart_router = APIRouter(
    prefix="/smart-folders", tags=["smart"], dependencies=[Depends(require_token)]
)


@smart_router.get("")
def list_smart() -> list[dict]:
    rows = (
        db()
        .execute(
            "SELECT id, name, query, filter_json, position FROM saved_searches "
            "WHERE deleted = 0 ORDER BY position, name"
        )
        .fetchall()
    )
    return [dict(r) for r in rows]


class SmartFolder(BaseModel):
    id: str | None = None
    name: str
    query: str
    filter_json: str = "{}"
    position: int = 0


@smart_router.put("")
def put_smart(folder: SmartFolder) -> dict:
    from rsscore.ids import new_id
    from rsscore.rules.smart import save_saved_search

    folder.id = folder.id or new_id()
    with write_tx() as conn:
        save_saved_search(conn, folder.model_dump())
    return folder.model_dump()


@smart_router.get("/{folder_id}/entries")
def smart_entries(folder_id: str, limit: int = 100) -> list[dict]:
    from rsscore.rules.smart import saved_search_to_selection

    conn = db()
    row = conn.execute("SELECT * FROM saved_searches WHERE id = ?", (folder_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Carpeta inteligente no encontrada")
    sel = saved_search_to_selection(conn, dict(row))
    sel.limit = limit
    return [
        {"id": e.id, "title": e.title, "url": e.url, "published_at": e.published_at}
        for e in repo.select_entries(conn, sel)
    ]


__all__ = ["config", "router", "smart_router"]
