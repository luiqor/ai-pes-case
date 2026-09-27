"""Pin the two layers of ``payloads.py`` and exercise the manifest boundary.

``study.json`` is the artifact the skill's contract lets users hand-edit, so
it is validated at every read. These tests cover that boundary (Pydantic) and
the meta-test that keeps the compile-time layer (TypedDict) in step with it.
"""

from __future__ import annotations

from typing import Any

import payloads
import pytest
from payloads import (
    CriteriaModel,
    CriteriaSpec,
    Resolution,
    ResolutionModel,
    StudyManifest,
    StudyManifestModel,
)


def test_pydantic_models_mirror_the_typed_dicts():
    """Both layers must describe the same field set, or they drift apart.

    A key added to a TypedDict without the matching model field (or vice
    versa) would make the runtime boundary accept a shape mypy rejects -- or
    reject the shape every stage actually writes.
    """
    for typed, model in (
        (StudyManifest, StudyManifestModel),
        (Resolution, ResolutionModel),
        (CriteriaSpec, CriteriaModel),
    ):
        assert set(typed.__annotations__) == set(model.model_fields), typed.__name__


def test_a_valid_manifest_passes_through_unchanged(study_factory):
    """Validation must return the original object, not a rebuilt copy."""
    study = study_factory()
    assert payloads.validate_study(study, "test") is study


def test_hand_edited_keys_survive_validation(study_factory):
    """Keys outside the schema are preserved for the next save.

    The model is used as a *validator* only; dumping it instead would drop
    the user's own notes from the manifest the next time run.py writes it.
    """
    study = study_factory()
    study["my_note"] = "scratch"
    assert payloads.validate_study(study, "test")["my_note"] == "scratch"


def test_a_corrupt_manifest_names_every_bad_field(study_factory):
    """The error must locate each problem, not just say 'invalid'."""
    study = study_factory()
    study["window"] = {"since": "banana", "until": "2015-13"}
    study["languages"] = ["PL"]
    with pytest.raises(SystemExit) as excinfo:
        payloads.validate_study(study, "manifest.json")
    message = str(excinfo.value)
    assert "manifest.json" in message
    assert "window.since" in message
    assert "window.until" in message
    assert "languages.0" in message


def test_missing_required_fields_are_rejected():
    with pytest.raises(SystemExit) as excinfo:
        payloads.validate_study({"topic": "only a topic"}, "manifest.json")
    message = str(excinfo.value)
    assert "languages" in message
    assert "Field required" in message


def test_json_that_is_not_an_object_is_rejected():
    """A list is valid JSON but not a manifest; it must not slip through."""
    with pytest.raises(SystemExit):
        payloads.validate_study(["not", "a", "manifest"], "manifest.json")


def test_manifest_that_is_not_json_explains_itself(tmp_path):
    """A truncated hand-edit fails with the path, not a raw traceback."""
    path = tmp_path / "study.json"
    path.write_text('{"topic": "x", "languages": [', encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        payloads.load_study_manifest(path)
    message = str(excinfo.value)
    assert "cannot read study manifest" in message
    assert str(path) in message


def test_wrong_enum_values_list_the_valid_ones(study_factory):
    """`access`/`agent`/`granularity` are URL path segments -- a wrong value
    must fail here (with the alternatives shown), not as a 404 at fetch."""
    study = study_factory()
    study["access"] = "all-ages"
    with pytest.raises(SystemExit) as excinfo:
        payloads.validate_study(study, "manifest.json")
    assert "all-access" in str(excinfo.value)


def test_version_must_stay_an_int(study_factory):
    study = study_factory()
    study["version"] = "banana"
    with pytest.raises(SystemExit) as excinfo:
        payloads.validate_study(study, "manifest.json")
    assert "version" in str(excinfo.value)


def test_report_language_is_optional_but_must_be_a_real_code(study_factory):
    """The field is opt-in (English by default) and follows the code pattern.

    It reaches the manifest from ``init --report-lang``, so a hand-edited
    ``study.json`` must be validated here rather than at render time.
    """
    study = study_factory()
    assert payloads.validate_study(study, "manifest.json") is study

    study["report_language"] = "pl"
    assert payloads.validate_study(study, "manifest.json")["report_language"] == "pl"

    study["report_language"] = "PL!"  # not a BCP-47-ish code this skill accepts
    with pytest.raises(SystemExit) as excinfo:
        payloads.validate_study(study, "manifest.json")
    assert "report_language" in str(excinfo.value)


def test_validate_study_accepts_a_resolved_manifest(resolve_study):
    """The shape resolve.py writes must satisfy the boundary it is saved to."""
    study: dict[str, Any] = resolve_study()
    assert payloads.validate_study(study, "test") is study


# ---------------------------------------------------------- data layers -----
def test_layers_and_criteria_pass_through_unchanged(study_factory):
    """The user's own words in ``criteria`` survive like any other note.

    Only ``rank_by`` is interpreted; the rest of the block (and the layer
    selection) is carried through untouched, exactly like hand-written keys.
    """
    study = study_factory()
    study["layers"] = ["access", "bot", "top"]
    study["criteria"] = {"rank_by": "bot_share", "note": "own words"}

    assert payloads.validate_study(study, "study.json") is study
    assert study["criteria"]["note"] == "own words"


def test_an_unknown_layer_lists_the_valid_ones(study_factory):
    """A layer name becomes a URL path segment -- reject it here, not as 404."""
    study = study_factory()
    study["layers"] = ["mobile"]

    with pytest.raises(SystemExit) as excinfo:
        payloads.validate_study(study, "study.json")
    message = str(excinfo.value)
    assert "layers.0" in message
    assert "access" in message


def test_an_unknown_rank_criterion_lists_the_valid_ones(study_factory):
    study = study_factory()
    study["criteria"] = {"rank_by": "popularity"}

    with pytest.raises(SystemExit) as excinfo:
        payloads.validate_study(study, "study.json")
    message = str(excinfo.value)
    assert "criteria.rank_by" in message
    assert "share_ppm" in message


def test_a_layer_bound_criterion_demands_its_layer(study_factory):
    """Ranking by a metric the study never fetches must refuse, not fall back.

    A report silently sorted by a *different* criterion than the one asked
    for would look correct and be wrong.
    """
    study = study_factory()
    study["criteria"] = {"rank_by": "bot_share"}

    with pytest.raises(SystemExit) as excinfo:
        payloads.validate_study(study, "study.json")
    message = str(excinfo.value)
    assert "criteria.rank_by" in message
    assert "'bot'" in message
    assert "layers" in message

    study["layers"] = ["bot"]
    assert payloads.validate_study(study, "study.json") is study


def test_mobile_share_is_tied_to_the_access_layer(study_factory):
    study = study_factory()
    study["criteria"] = {"rank_by": "mobile_share"}

    with pytest.raises(SystemExit) as excinfo:
        payloads.validate_study(study, "study.json")
    assert "'access'" in str(excinfo.value)


def test_a_layer_free_criterion_needs_no_layer(study_factory):
    """The default criteria keep working for a study that fetches no layers."""
    study = study_factory()
    study["criteria"] = {"rank_by": "share_ppm"}
    assert payloads.validate_study(study, "study.json") is study


# ------------------------------------------------------- success criteria ---
def _rule(**overrides: Any) -> dict[str, Any]:
    """A valid ``criteria.success`` entry; tests override one field at a time."""
    rule: dict[str, Any] = {
        "id": "growing",
        "metric": "yoy_share_pct",
        "op": ">=",
        "value": -5,
    }
    rule.update(overrides)
    return rule


def test_a_valid_success_rule_passes_through_unchanged(study_factory):
    """Validation must return the original object, label included."""
    study = study_factory()
    study["criteria"] = {"success": [_rule(label="share ≥ −5%")]}

    assert payloads.validate_study(study, "study.json") is study
    assert study["criteria"]["success"][0]["label"] == "share ≥ −5%"


def test_an_unknown_success_metric_lists_the_valid_ones(study_factory):
    """A metric becomes a dict lookup at analyze time -- refuse it here."""
    study = study_factory()
    study["criteria"] = {"success": [_rule(metric="popularity")]}

    with pytest.raises(SystemExit) as excinfo:
        payloads.validate_study(study, "study.json")
    message = str(excinfo.value)
    assert "criteria.success.0.metric" in message
    assert "share_ppm" in message


def test_an_unknown_success_operator_lists_the_valid_ones(study_factory):
    study = study_factory()
    study["criteria"] = {"success": [_rule(op="!=")]}

    with pytest.raises(SystemExit) as excinfo:
        payloads.validate_study(study, "study.json")
    message = str(excinfo.value)
    assert "criteria.success.0.op" in message
    assert ">=" in message


def test_a_confidence_rule_takes_a_grade_not_a_number(study_factory):
    """``confidence`` is ordinal; a number there would compare nothing."""
    study = study_factory()
    study["criteria"] = {"success": [_rule(metric="confidence", value="medium")]}
    assert payloads.validate_study(study, "study.json") is study

    study["criteria"] = {"success": [_rule(metric="confidence", value=2)]}
    with pytest.raises(SystemExit) as excinfo:
        payloads.validate_study(study, "study.json")
    message = str(excinfo.value)
    assert "criteria.success.0.value" in message
    assert "'medium'" in message


def test_a_numeric_rule_rejects_a_grade_string(study_factory):
    study = study_factory()
    study["criteria"] = {"success": [_rule(value="medium")]}

    with pytest.raises(SystemExit) as excinfo:
        payloads.validate_study(study, "study.json")
    assert "value must be a number" in str(excinfo.value)


def test_a_duplicate_rule_id_is_refused(study_factory):
    """Two rules sharing an id would collide in the verdict map."""
    study = study_factory()
    study["criteria"] = {"success": [_rule(), _rule(metric="share_ppm")]}

    with pytest.raises(SystemExit) as excinfo:
        payloads.validate_study(study, "study.json")
    message = str(excinfo.value)
    assert "criteria.success.1" in message
    assert "duplicate id 'growing'" in message


def test_a_rule_id_must_be_a_slug(study_factory):
    """The id addresses the verdict in JSON and prose; keep it addressable."""
    study = study_factory()
    study["criteria"] = {"success": [_rule(id="Growing?")]}

    with pytest.raises(SystemExit) as excinfo:
        payloads.validate_study(study, "study.json")
    assert "criteria.success.0.id" in str(excinfo.value)


def test_a_layer_bound_success_rule_demands_its_layer(study_factory):
    """Grading a metric the study never fetches must refuse, not fall back."""
    study = study_factory()
    study["criteria"] = {"success": [_rule(metric="mobile_pct", value=60)]}

    with pytest.raises(SystemExit) as excinfo:
        payloads.validate_study(study, "study.json")
    message = str(excinfo.value)
    assert "criteria.success.0" in message
    assert "'access'" in message
    assert "layers" in message

    study["layers"] = ["access"]
    assert payloads.validate_study(study, "study.json") is study
