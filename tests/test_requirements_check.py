"""Policy tests for the deterministic requirement check -- no Ollama calls."""
from mail_agent import config, cv_matcher, llm_client, requirements_check as rc

CV = "Senior Data Scientist. Python, SQL, C++, PyTorch, machine learning, semiconductor process control. BSc."


def _req(text, kind, necessity="required", any_of=None):
    return {"text": text, "kind": kind, "necessity": necessity, "any_of": any_of or []}


class TestEvaluate:
    def test_sole_missing_language_blocks(self):
        blocking, _ = rc.evaluate([_req("Java experience", "language", any_of=["Java"])], CV)
        assert blocking == ["Java experience"]

    def test_any_listed_alternative_satisfies(self):
        reqs = [_req("Python, C++ or Java", "language", any_of=["Python", "C++", "Java"])]
        assert rc.evaluate(reqs, CV) == ([], [])

    def test_missing_domain_expertise_blocks(self):
        reqs = [_req("Experience in chip design", "domain", any_of=["chip design", "ASIC", "SoC"])]
        assert rc.evaluate(reqs, CV)[0] == ["Experience in chip design"]

    def test_degree_years_and_preferred_never_block(self):
        reqs = [
            _req("MSc or PhD", "degree", any_of=["MSc", "PhD"]),
            _req("8+ years", "years", any_of=["8 years"]),
            _req("Rust", "language", necessity="preferred", any_of=["Rust"]),
        ]
        blocking, other = rc.evaluate(reqs, CV)
        assert blocking == [] and len(other) == 3

    def test_alias_and_word_boundaries(self):
        assert rc.evaluate([_req("ML", "domain", any_of=["ML"])], CV) == ([], [])
        # "C" must not match inside "C++"/"Scientist"; "Go" must not match "Google"
        assert rc.evaluate([_req("Go", "language", any_of=["Go"])], "Worked at Google")[0] == ["Go"]


class TestScoreIntegration:
    def _score(self, monkeypatch, tmp_path, requirements, extra="", body="chip design experience, MSc"):
        cv = tmp_path / "cv.txt"
        cv.write_text(CV, encoding="utf-8")
        monkeypatch.setattr(config, "CV_TEXT_PATH", str(cv))
        monkeypatch.setattr(cv_matcher, "ensure_profile", lambda: {})
        monkeypatch.setattr(llm_client, "call_json", lambda p, **k: {
            "score": 73, "requirements": requirements, "summary": "ok"})
        content = "Requirements:\n- " + body + "\nResponsibilities:\n- build things\nQualifications: x"
        return cv_matcher.score_job_email("Acme", "Engineer", content)

    def test_missing_required_domain_is_capped_below_threshold(self, monkeypatch, tmp_path):
        r = self._score(monkeypatch, tmp_path, [_req("chip design", "domain", any_of=["chip design"])])
        assert r["score"] == config.HARD_REQUIREMENT_SCORE_CAP < config.FIT_SCORE_THRESHOLD

    def test_degree_gap_keeps_score(self, monkeypatch, tmp_path):
        r = self._score(monkeypatch, tmp_path, [_req("MSc", "degree", "must_have", ["MSc"])], body="MSc degree and Python")
        assert r["score"] >= config.FIT_SCORE_THRESHOLD  # a degree gap alone never drops a role


def test_requirement_without_keywords_is_unverifiable_not_blocking():
    assert rc.evaluate([_req("Machine learning and data analysis experience", "technology")], CV) == \
        ([], ["Machine learning and data analysis experience"])


class TestComputeScore:
    def test_all_must_haves_met_scores_high(self):
        reqs = [_req("Python", "language", "must_have", ["Python"]), _req("ML", "domain", "must_have", ["machine learning"])]
        assert rc.compute_score(reqs, CV) >= 85

    def test_degree_gap_is_mild(self):
        base = [_req("Python", "language", "must_have", ["Python"])]
        with_degree = base + [_req("MSc", "degree", "must_have", ["MSc"])]
        assert rc.compute_score(base, CV) - rc.compute_score(with_degree, CV) <= 15

    def test_skill_gap_is_heavy(self):
        reqs = [_req("Python", "language", "must_have", ["Python"]), _req("Rust", "language", "must_have", ["Rust"])]
        assert rc.compute_score(reqs, CV) < 65

    def test_nothing_checkable_returns_none(self):
        assert rc.compute_score([_req("vague", "technology", "must_have")], CV) is None


def test_annotate_marks_coverage_and_level():
    out = rc.annotate([_req("Python", "language", "must_have", ["Python"]),
                       _req("Rust", "language", "nice_to_have", ["Rust"]),
                       _req("vague", "technology", "must_have")], CV)
    assert [(o["skill"], o["level"], o["met"]) for o in out] == [
        ("Python", "must_have", True), ("Rust", "nice_to_have", False), ("vague", "must_have", None)]


class TestKeywordsMustComeFromThePosting:
    def test_cv_skill_not_in_posting_is_ignored(self):
        # Model copied "Pandas" from the CV profile into any_of; the posting never says it.
        reqs = [_req("data tools", "technology", "must_have", ["Pandas"])]
        assert rc.compute_score(reqs, "Pandas, Python", "We need a great engineer") is None

    def test_snake_case_and_long_phrases_are_dropped(self):
        reqs = [_req("x", "technology", "must_have", ["built_with_ai"])]
        assert rc.evaluate(reqs, CV, "you have built with ai in production") == ([], ["x"])

    def test_hyphen_and_space_variants_match(self):
        assert rc.evaluate([_req("t", "technology", "must_have", ["time-series"])],
                           "worked on time series forecasting", "time-series modeling") == ([], [])

    def test_score_never_reaches_100(self):
        reqs = [_req("Python", "language", "must_have", ["Python"]), _req("SQL", "technology", "nice_to_have", ["SQL"])]
        assert rc.compute_score(reqs, CV + " SQL", "Python and SQL") <= 95


class TestReasoning:
    def test_breakdown_and_explanation_name_blocker_and_components(self):
        reqs = [_req("Python", "language", "must_have", ["Python"]),
                _req("AWS", "technology", "must_have", ["AWS"]),
                _req("MSc", "degree", "must_have", ["MSc"]),
                _req("SQL", "language", "nice_to_have", ["SQL"])]
        post = "Python AWS MSc SQL"
        bd = rc.score_breakdown(reqs, CV + " SQL", post)
        assert (bd["skill_met"], bd["skill_total"], bd["nice_met"], bd["nice_total"]) == (1, 2, 1, 1)
        assert bd["penalties"] == [{"requirement": "MSc", "points": 4}]
        blockers, _ = rc.evaluate(reqs, CV + " SQL", post)
        text = "\n".join(rc.explain(bd, blockers, 25, model_score=73))
        assert "HARD BLOCKER" in text and "AWS" in text and "1/2" in text
        assert "-4" in text and "capped" in text and "73" in text
