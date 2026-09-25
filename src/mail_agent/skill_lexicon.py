"""Deterministic skill extraction: a curated lexicon scanned against a posting's text.

The LLM extraction alone was unreliable -- it sometimes forgot to list "AWS" or "OPC UA /
SCADA" at all, and the same posting gave different lists on different runs, so a verdict
could flip from 25 to 95. A lexicon scan is fully repeatable and never forgets a term it
knows. The LLM still runs for the long tail (free-text domain requirements) and its items
are merged in, de-duplicated against these.

Grow it from `scripts/calibrate_scoring.py`: the report lists the unmet must-haves in the
postings you rejected -- any that keep appearing and aren't here should be added.

Each entry: canonical name -> (kind, [synonyms that would appear in a posting or a CV]).
Kinds: language | technology | domain.
"""
from __future__ import annotations

import re

LEXICON: dict[str, tuple[str, list[str]]] = {
    # --- programming languages ---
    "Java": ("language", ["java"]),
    "JavaScript": ("language", ["javascript", "node.js", "nodejs"]),
    "TypeScript": ("language", ["typescript"]),
    "C#": ("language", ["c#", ".net", "dotnet"]),
    "C++": ("language", ["c++"]),
    "C": ("language", ["embedded c"]),
    "Go": ("language", ["golang"]),
    "Rust": ("language", ["rust"]),
    "Scala": ("language", ["scala"]),
    "Kotlin": ("language", ["kotlin"]),
    "Swift": ("language", ["swiftui", "swift developer", "ios swift"]),
    "Ruby": ("language", ["ruby"]),
    "PHP": ("language", ["php"]),
    "MATLAB": ("language", ["matlab"]),
    "Python": ("language", ["python"]),
    "SQL": ("language", ["sql"]),
    "Verilog/SystemVerilog": ("language", ["systemverilog", "verilog", "vhdl"]),
    # --- cloud / infrastructure ---
    "AWS": ("technology", ["aws", "amazon web services"]),
    "Azure": ("technology", ["azure"]),
    "GCP": ("technology", ["gcp", "google cloud platform"]),
    "Cloud": ("technology", ["cloud native", "cloud computing", "cloud infrastructure", "cloud based development"]),
    "Kubernetes": ("technology", ["kubernetes", "k8s"]),
    "Docker": ("technology", ["docker"]),
    "Terraform": ("technology", ["terraform"]),
    "CI/CD": ("technology", ["ci/cd", "ci cd", "jenkins", "github actions"]),
    "Linux": ("technology", ["linux"]),
    # --- data / backend ---
    "Spark": ("technology", ["spark", "pyspark"]),
    "Kafka": ("technology", ["kafka"]),
    "Airflow": ("technology", ["airflow"]),
    "Hadoop": ("technology", ["hadoop"]),
    "Snowflake": ("technology", ["snowflake"]),
    "Databricks": ("technology", ["databricks"]),
    "NoSQL": ("technology", ["nosql", "mongodb", "cassandra", "dynamodb"]),
    "React": ("technology", ["reactjs", "react js", "react native", "angular", "vue js", "vuejs"]),
    "Spring": ("technology", ["spring boot"]),
    "REST/microservices": ("technology", ["microservices", "rest api", "restful"]),
    # --- ML / AI ---
    "TensorFlow": ("technology", ["tensorflow", "keras"]),
    "PyTorch": ("technology", ["pytorch"]),
    "OpenCV": ("technology", ["opencv"]),
    "CUDA": ("technology", ["cuda", "gpu programming"]),
    "Deep learning": ("domain", ["deep learning", "neural network"]),
    "Computer vision": ("domain", ["computer vision", "image processing"]),
    "NLP": ("domain", ["nlp", "natural language processing"]),
    "LLM": ("domain", ["llm", "large language model"]),
    "Reinforcement learning": ("domain", ["reinforcement learning"]),
    "Large-scale model training": ("domain", [
        "large scale training", "large scale models", "training large", "foundation model",
        "distributed training", "pretraining", "pre training", "model pretraining"]),
    "Time series": ("domain", ["time series"]),
    "Generative AI / agents": ("domain", ["agentic", "ai agents", "multi agent", "langchain", "langgraph"]),
    "MLOps": ("technology", ["mlops", "mlflow", "kubeflow"]),
    # --- industrial / embedded / hardware ---
    "OPC UA": ("technology", ["opc ua", "opc-ua", "opc"]),
    "PLC": ("technology", ["plc", "plcs"]),
    "SCADA": ("technology", ["scada"]),
    "Modbus": ("technology", ["modbus", "profibus", "profinet"]),
    "MES / industrial automation": ("domain", ["industrial automation", "manufacturing execution"]),
    "ROS / robotics": ("domain", ["ros2", "robot operating system", "robotics"]),
    "Embedded": ("domain", ["embedded", "firmware", "rtos"]),
    "FPGA": ("technology", ["fpga"]),
    "Chip design / verification": ("domain", ["chip design", "asic", "soc", "rtl", "uvm", "physical design"]),
    "Signal processing / radar": ("domain", ["signal processing", "radar", "dsp"]),
    "Cybersecurity": ("domain", ["cybersecurity", "cyber security", "penetration testing"]),
    "Bioinformatics": ("domain", ["bioinformatics", "genomics"]),
}

_NICE_CUES = re.compile(
    r"\b(?:preferred|nice[ -]to[ -]have|advantage|a[ \t]+plus|bonus|desirable|desired|beneficial|"
    r"is[ \t]+an[ \t]+asset|would[ \t]+be[ \t]+a)\b", re.IGNORECASE)
_NICE_HEADING = re.compile(
    r"^[ \t]*(?:preferred|nice[ -]to[ -]have|advantages?|bonus|desirable|plus)[^\n]{0,40}$", re.IGNORECASE)
_MUST_HEADING = re.compile(
    r"^[ \t]*(?:requirements?|(?:minimum|basic|key|required)[ \t]+qualifications?|qualifications?|"
    r"what[ \t]+we(?:'|’)?re[ \t]+looking[ \t]+for|who[ \t]+you[ \t]+are|responsibilit\w*|job[ \t]+description)[^\n]{0,40}$",
    re.IGNORECASE)
_ALT_SEPARATORS = re.compile(r"\bor\b|/|,|\band/or\b", re.IGNORECASE)


def _norm(text: str) -> str:
    return re.sub(r"[-_]+", " ", text.lower())


def _find_terms(line_norm: str) -> list[tuple[str, str]]:
    """(canonical, kind) for every lexicon entry mentioned in a normalised line."""
    found = []
    for canonical, (kind, synonyms) in LEXICON.items():
        for syn in synonyms:
            if re.search(rf"(?<![\w+#]){re.escape(_norm(syn))}(?![\w+#])", line_norm):
                found.append((canonical, kind))
                break
    return found


def extract_requirements(text: str) -> list[dict]:
    """Requirements the lexicon finds in `text`, in the same shape the LLM extraction
    produces: {text, kind, necessity, any_of}. Necessity comes from the nearest section
    heading (Preferred/Advantage -> nice_to_have) or an inline cue on the same line.
    Several languages on one line joined by 'or' / '/' / ',' are ONE requirement with
    alternatives (any one satisfies it), e.g. "Python, C++ or Java"."""
    section_nice = False
    merged: dict[str, dict] = {}
    for raw in (text or "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _NICE_HEADING.match(line):
            section_nice = True
            continue
        if _MUST_HEADING.match(line):
            section_nice = False
            continue
        line_norm = _norm(line)
        terms = _find_terms(line_norm)
        if not terms:
            continue
        nice = section_nice or bool(_NICE_CUES.search(line))
        langs = [c for c, k in terms if k == "language"]
        group = langs if len(langs) > 1 and _ALT_SEPARATORS.search(line) else []
        for canonical, kind in terms:
            key = "|".join(sorted(group)) if canonical in group else canonical
            level = "nice_to_have" if nice else "must_have"
            existing = merged.get(key)
            if existing:
                if level == "must_have":  # a must-have mention anywhere wins
                    existing["necessity"] = "must_have"
                continue
            if canonical in group:
                any_of = sorted({s for c in group for s in LEXICON[c][1]})
                label = " / ".join(sorted(group))
            else:
                any_of = list(LEXICON[canonical][1])
                label = canonical
            merged[key] = {"text": label, "kind": kind, "necessity": level, "any_of": any_of, "source": "lexicon"}
    return list(merged.values())


def merge_requirements(lexicon_reqs: list[dict], llm_reqs: list[dict]) -> list[dict]:
    """Lexicon items first (authoritative for what they cover); an LLM item is kept only
    when none of its keywords overlap a lexicon item's -- i.e. it adds something new."""
    covered = {_norm(k) for r in lexicon_reqs for k in r.get("any_of", [])}
    extra = []
    for req in llm_reqs or []:
        if not isinstance(req, dict):
            continue
        keys = {_norm(str(k)) for k in (req.get("any_of") or [])}
        if keys and keys & covered:
            continue
        extra.append(req)
    return lexicon_reqs + extra
