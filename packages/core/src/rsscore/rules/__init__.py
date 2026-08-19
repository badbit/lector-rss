"""Motor de reglas: esquema declarativo, evaluación, persistencia y carpetas inteligentes."""

from .apply import RuleOutcome, apply_rules, make_ingest_hook
from .engine import RuleEngine, fold
from .models import (
    Action,
    ActionKind,
    Condition,
    ConditionGroup,
    ExportSpec,
    NotifySpec,
    Op,
    Rule,
    RuleField,
    Scope,
    parse_rules,
)
from .smart import SavedSearch, SavedSearchFilter, run_saved_search, saved_search_to_selection
from .store import (
    delete_rule,
    export_rules_yaml,
    get_rule,
    import_rules_yaml,
    load_rules,
    save_rule,
)

__all__ = [
    "Action",
    "ActionKind",
    "Condition",
    "ConditionGroup",
    "ExportSpec",
    "NotifySpec",
    "Op",
    "Rule",
    "RuleEngine",
    "RuleField",
    "RuleOutcome",
    "SavedSearch",
    "SavedSearchFilter",
    "Scope",
    "apply_rules",
    "delete_rule",
    "export_rules_yaml",
    "fold",
    "get_rule",
    "import_rules_yaml",
    "load_rules",
    "make_ingest_hook",
    "parse_rules",
    "run_saved_search",
    "save_rule",
    "saved_search_to_selection",
]
