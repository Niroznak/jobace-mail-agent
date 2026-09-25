"""Deterministic lexicon extraction -- no Ollama calls."""
from mail_agent import requirements_check as rc
from mail_agent import skill_lexicon as lx

CV = "Senior Data Scientist. Python, SQL, C++, PyTorch, Docker, deep learning, computer vision, LLM."


def _by_text(reqs):
    return {r["text"]: r for r in reqs}


def test_finds_cloud_and_industrial_terms_the_llm_used_to_miss():
    post = "Requirements:\n- Experience with AWS or Azure cloud services\n- Knowledge of OPC UA, PLCs and SCADA systems"
    got = _by_text(lx.extract_requirements(post))
    assert {"AWS", "Azure", "OPC UA", "PLC", "SCADA"} <= set(got)
    assert all(r["necessity"] == "must_have" for r in got.values())


def test_language_alternatives_are_one_requirement():
    got = lx.extract_requirements("Requirements:\n- Proficiency in Python, C++ or Java")
    assert len(got) == 1 and got[0]["kind"] == "language"
    assert rc.evaluate(got, CV, "Python, C++ or Java") == ([], [])  # Python/C++ satisfy it


def test_sole_language_is_a_blocker():
    got = lx.extract_requirements("Requirements:\n- Strong Java expertise")
    assert rc.evaluate(got, CV, "Strong Java expertise")[0] == ["Java"]


def test_preferred_section_and_inline_cues_make_nice_to_have():
    post = "Requirements:\n- Python\nPreferred Qualifications:\n- Kubernetes\n- Kafka\nMore:\n- Rust is a plus"
    got = _by_text(lx.extract_requirements(post))
    assert got["Python"]["necessity"] == "must_have"
    assert got["Kubernetes"]["necessity"] == "nice_to_have" and got["Kafka"]["necessity"] == "nice_to_have"


def test_large_scale_training_is_distinct_from_plain_deep_learning():
    post = "Requirements:\n- Strong foundation in deep learning theory and experience training large scale models"
    got = _by_text(lx.extract_requirements(post))
    assert "Deep learning" in got and "Large-scale model training" in got
    blocking, _ = rc.evaluate(list(got.values()), CV, post)
    assert blocking == ["Large-scale model training"]  # CV has deep learning, not large-scale training


def test_no_false_positive_on_common_words():
    got = lx.extract_requirements("Requirements:\n- React quickly to change and work in a swift, spring-like pace")
    assert got == []


def test_merge_drops_llm_items_the_lexicon_already_covers_and_keeps_new_ones():
    lex = lx.extract_requirements("Requirements:\n- Python")
    llm = [{"text": "Python dev", "kind": "language", "necessity": "must_have", "any_of": ["python"]},
           {"text": "Wafer metrology", "kind": "domain", "necessity": "must_have", "any_of": ["metrology"]}]
    merged = lx.merge_requirements(lex, llm)
    assert [r["text"] for r in merged] == ["Python", "Wafer metrology"]
