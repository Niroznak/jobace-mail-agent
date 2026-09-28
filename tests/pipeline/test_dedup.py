"""Tests for the title-tier dedup key -- no network/Ollama calls."""
from mail_agent.pipeline import dedup


class TestJobIdForCompanyNormalization:
    def test_parenthetical_company_aside_does_not_split_the_key(self):
        # Real bug: a reply naming "CaliAlfa" didn't match the original row saved as
        # "CaliAlfa (Previously Alfabet)" -- two different dedup keys for one posting,
        # so the reply opened a duplicate row instead of updating the existing one.
        a = dedup.job_id_for("CaliAlfa (Previously Alfabet)", "Senior Data Scientist")
        b = dedup.job_id_for("CaliAlfa", "Senior Data Scientist")
        assert a == b

    def test_corporate_suffix_does_not_split_the_key(self):
        a = dedup.job_id_for("Micron", "Algorithm Engineer")
        b = dedup.job_id_for("Micron Technology", "Algorithm Engineer")
        assert a == b

    def test_genuinely_different_companies_still_differ(self):
        a = dedup.job_id_for("Meta", "Software Engineer")
        b = dedup.job_id_for("Metadata Inc", "Software Engineer")
        assert a != b

    def test_position_id_takes_priority_over_company_title(self):
        a = dedup.job_id_for("CaliAlfa", "Role A", position_id="123")
        b = dedup.job_id_for("Totally Different Co", "Role B", position_id="123")
        assert a == b
