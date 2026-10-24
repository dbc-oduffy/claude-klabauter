"""Import-free leaf holding the size-to-route table shared by sizing_assemble and ask_fanout."""

# HARD GATE: adding a KEY for a new size (XXL is the latest) extends this table.
_BASE_ROUTE_BY_TSHIRT = {
    "XS": "dispatch",
    "S": "spec-dispatch",
    "M": "plan",
    "L": "plan",
    "XL": "pm-decision",
    "XXL": "goal-setting",
}
