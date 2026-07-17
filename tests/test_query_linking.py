"""Why: QP1's auto graph plan is what lets a PLAIN-LANGUAGE caller reach the
GraphRAG core at all — before it, graph mode required template+seed knowledge
no NL caller has. The linking must be deterministic and auditable (an LLM
guess here would put prose in charge of traversal — the §21 guardrail is
templates + SoR-resolved seeds only), match with the ONE frozen normalization
(class 5: a second rule drifts from the identity keys), and emit STORED names
so ``entity_ids_by_name`` resolves every plan it produces by construction."""

from __future__ import annotations

from core.query.linking import GraphPlan, derive_plan, link_names


def test_links_by_normalized_containment() -> None:
    """Width (NFKC), case and whitespace differences must not break a link —
    the SAME rule identity keys use, so a name the build stores is found
    however the question spells it."""
    names = ["People Ops", "區域探索廳"]
    assert link_names("who runs people   ops now?", names) == ["People Ops"]
    assert link_names("PEOPLE OPS 歸誰管?", names) == ["People Ops"]
    # full-width letters fold to the stored half-width name via NFKC
    assert link_names("Ｐｅｏｐｌｅ Ｏｐｓ 在哪?", names) == ["People Ops"]
    assert link_names("區域探索廳有什麼可以看的?", names) == ["區域探索廳"]
    assert link_names("nothing relevant here", names) == []


def test_single_character_names_never_link() -> None:
    """A one-char name (「廳」) appears inside countless unrelated questions —
    linking it would fire the graph mode on noise, not on a mention."""
    assert link_names("這個廳在哪裡?", ["廳"]) == []


def test_shadowed_substring_names_yield_to_the_longest() -> None:
    """「區域」 is contained in 「區域探索廳」: when both match, only the
    longest (most specific) name links — a generic fragment must not become a
    second traversal seed."""
    names = ["區域", "區域探索廳"]
    assert link_names("區域探索廳有什麼?", names) == ["區域探索廳"]
    # the short name still links when it stands alone
    assert link_names("這個區域的氣候如何?", names) == ["區域"]


def test_question_order_becomes_path_direction() -> None:
    """The path template reads src → dst; the question's own order (「從 A 到
    B」) is the only deterministic signal for which is which."""
    names = ["海科館", "區域探索廳"]
    plan = derive_plan("從海科館怎麼走到區域探索廳?", names, max_graph_hops=3)
    assert plan is not None
    assert plan.params.template == "path"
    assert plan.params.entity == "海科館"
    assert plan.params.other_entity == "區域探索廳"
    assert plan.params.hops == 3  # a path plan gets the §21 ceiling — it exists
    # to FIND the chain, and _validate_params accepts hops == max


def test_one_entity_plans_cheap_neighbors() -> None:
    plan = derive_plan("區域探索廳有什麼可以看的?", ["區域探索廳"], max_graph_hops=3)
    assert plan is not None
    assert plan.params.template == "neighbors"
    assert plan.params.entity == "區域探索廳"
    assert plan.params.other_entity is None
    assert plan.params.hops == 1  # the cheapest sanctioned look around the seed


def test_no_link_means_no_plan() -> None:
    assert derive_plan("完全無關的問題", ["區域探索廳"], max_graph_hops=3) is None


def test_extra_linked_names_are_counted_in_the_note() -> None:
    """More than two links: the plan still uses the first two (path), but the
    trace must admit what else linked — a silent drop would misrepresent the
    router's reading of the question."""
    names = ["海科館", "區域探索廳", "潮境智能海洋館"]
    plan = derive_plan("海科館的區域探索廳和潮境智能海洋館有關嗎?", names, max_graph_hops=2)
    assert plan is not None
    assert plan.params.template == "path"
    assert plan.linked_names == ("海科館", "區域探索廳", "潮境智能海洋館")
    assert "+1 more linked" in plan.note


def test_emits_stored_spellings_and_an_auditable_note() -> None:
    """The seed must be the STORED canonical name (entity_ids_by_name matches
    lower(stored)) — emitting the normalized form could miss; and the note is
    the routing-trace audit line, so it names template and seed."""
    plan = derive_plan("who runs PEOPLE OPS?", ["People Ops"], max_graph_hops=3)
    assert plan is not None
    assert plan.params.entity == "People Ops"  # stored spelling, not "people ops"
    assert isinstance(plan, GraphPlan)
    assert "neighbors" in plan.note and "People Ops" in plan.note


def test_normalization_equal_spellings_dedupe_to_the_earliest() -> None:
    """Two stored spellings that normalize equal are ONE link target — they
    resolve to the same lower() bucket downstream; listing both would fake a
    two-entity path out of one real-world thing."""
    names = ["People Ops", "people ops"]
    plan = derive_plan("people ops report?", names, max_graph_hops=3)
    assert plan is not None
    assert plan.params.template == "neighbors"
    assert len(plan.linked_names) == 1
