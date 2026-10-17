"""
South India Travel Graph Chatbot
Streamlit UI + Neo4j backend.  Run:  streamlit run app.py   (opens http://localhost:8501)

Credentials are NEVER hardcoded. Put them in a local .env file (see .env.example)
or in .streamlit/secrets.toml. Both are git-ignored.
"""
import os
import re

import streamlit as st
from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

# ---------------------------------------------------------------------------
# Config  (edit the schema names below to match your load_data_cypher.txt)
# ---------------------------------------------------------------------------
CITY_LABEL = "City"
ATTRACTION_LABEL = "Attraction"
TAG_LABEL = "Tag"
LOCATED_REL = "LOCATED_IN"      # (Attraction)-[:LOCATED_IN]->(City)
TAG_REL = "HAS_TAG"             # (Attraction)-[:HAS_TAG]->(Tag)
NAME_PROP = "name"
COST_PROP = "cost"              # entry cost in rupees


def get_setting(key: str, default: str | None = None) -> str | None:
    value = os.getenv(key)
    if value:
        return value
    try:
        return st.secrets[key]
    except Exception:
        return default


NEO4J_URI = get_setting("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = get_setting("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = get_setting("NEO4J_PASSWORD")
NEO4J_DATABASE = get_setting("NEO4J_DATABASE", "neo4j")
ANTHROPIC_API_KEY = get_setting("ANTHROPIC_API_KEY")  # optional


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_driver():
    if not NEO4J_PASSWORD:
        raise RuntimeError("NEO4J_PASSWORD is not set. Add it to your .env file.")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    driver.verify_connectivity()
    return driver


def run_query(cypher: str, **params):
    driver = get_driver()
    records, _, _ = driver.execute_query(cypher, params, database_=NEO4J_DATABASE)
    return [r.data() for r in records]


@st.cache_data(ttl=300, show_spinner=False)
def load_cities() -> list[str]:
    rows = run_query(f"MATCH (c:{CITY_LABEL}) RETURN c.{NAME_PROP} AS n ORDER BY n")
    return [r["n"] for r in rows if r["n"]]


@st.cache_data(ttl=300, show_spinner=False)
def load_tags() -> list[str]:
    rows = run_query(f"MATCH (t:{TAG_LABEL}) RETURN t.{NAME_PROP} AS n ORDER BY n")
    return [r["n"] for r in rows if r["n"]]


@st.cache_data(ttl=300, show_spinner=False)
def load_schema() -> dict:
    labels = [r["label"] for r in run_query("CALL db.labels() YIELD label RETURN label")]
    rels = [r["t"] for r in run_query("CALL db.relationshipTypes() YIELD relationshipType AS t RETURN t")]
    return {"labels": labels, "relationships": rels}


# ---------------------------------------------------------------------------
# Rule-based question answering (works with no LLM / API key)
# ---------------------------------------------------------------------------
def find_mentions(text: str, options: list[str]) -> list[str]:
    """Return options mentioned in text, in the order they appear."""
    low = text.lower()
    hits = [(low.find(o.lower()), o) for o in options if o.lower() in low]
    return [o for _, o in sorted(hits)]


def format_attractions(rows) -> str:
    if not rows:
        return "I couldn't find any places matching that."
    lines = []
    for r in rows:
        cost = r.get("cost")
        cost_txt = "Free" if cost in (0, None) else f"₹{cost}"
        tags = ", ".join(r.get("tags") or [])
        place = f" ({r['city']})" if r.get("city") else ""
        lines.append(f"- **{r['name']}**{place}: {cost_txt}" + (f" · {tags}" if tags else ""))
    return "\n".join(lines)


def answer_rule_based(question: str) -> str | None:
    q = question.lower()
    cities = find_mentions(question, load_cities())
    tags = find_mentions(question, load_tags())
    nums = [int(n) for n in re.findall(r"\d+", q)]
    max_cost = nums[0] if nums and any(w in q for w in ("under", "below", "less than", "budget", "within", "₹", "rs")) else None
    free_only = "free" in q

    # Route / connection between two cities
    if len(cities) >= 2 and any(w in q for w in ("from", "to", "route", "reach", "travel", "go", "between", "distance")):
        rows = run_query(
            f"""MATCH (a:{CITY_LABEL})-[r]-(b:{CITY_LABEL})
                WHERE toLower(a.{NAME_PROP}) = $a AND toLower(b.{NAME_PROP}) = $b
                RETURN type(r) AS type, properties(r) AS props""",
            a=cities[0].lower(), b=cities[1].lower(),
        )
        if not rows:
            return f"I don't see a direct link between **{cities[0]}** and **{cities[1]}** in the graph."
        out = [f"Direct connections between **{cities[0]}** and **{cities[1]}**:"]
        for r in rows:
            props = ", ".join(f"{k}: {v}" for k, v in r["props"].items())
            out.append(f"- {r['type']}" + (f" ({props})" if props else ""))
        return "\n".join(out)

    # Places in a city (optionally filtered by tag / cost)
    if cities or tags or free_only or max_cost is not None:
        where, params = [], {}
        if cities:
            where.append(f"toLower(c.{NAME_PROP}) IN $cities")
            params["cities"] = [c.lower() for c in cities]
        if tags:
            where.append(f"toLower(t.{NAME_PROP}) IN $tags")
            params["tags"] = [t.lower() for t in tags]
        if free_only:
            where.append(f"coalesce(a.{COST_PROP}, 0) = 0")
        elif max_cost is not None:
            where.append(f"coalesce(a.{COST_PROP}, 0) <= $max_cost")
            params["max_cost"] = max_cost
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = run_query(
            f"""MATCH (a:{ATTRACTION_LABEL})-[:{LOCATED_REL}]->(c:{CITY_LABEL})
                OPTIONAL MATCH (a)-[:{TAG_REL}]->(t:{TAG_LABEL})
                WITH a, c, collect(DISTINCT t.{NAME_PROP}) AS tag_list
                WITH a, c, tag_list
                {clause.replace('t.' + NAME_PROP, 'tag_list')} 
                RETURN a.{NAME_PROP} AS name, c.{NAME_PROP} AS city,
                       a.{COST_PROP} AS cost, tag_list AS tags
                ORDER BY cost, name LIMIT 25""".replace(
                "toLower(tag_list) IN $tags", "any(x IN tag_list WHERE toLower(x) IN $tags)"
            ),
            **params,
        )
        return format_attractions(rows)

    if any(w in q for w in ("cities", "city list", "which cities", "destinations")):
        return "Cities in the graph:\n" + ", ".join(load_cities())
    if "tag" in q or "categories" in q or "types" in q:
        return "Available tags: " + ", ".join(load_tags())
    return None


# ---------------------------------------------------------------------------
# Optional: LLM text-to-Cypher fallback (only if ANTHROPIC_API_KEY is set)
# ---------------------------------------------------------------------------
WRITE_WORDS = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|CALL\s+dbms|LOAD\s+CSV|FOREACH)\b", re.I)


def answer_with_llm(question: str) -> str | None:
    if not ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    schema = load_schema()
    model = get_setting("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    sys_prompt = (
        "You write a single READ-ONLY Neo4j Cypher query answering the user's question "
        f"about a South India travel graph.\nLabels: {schema['labels']}\n"
        f"Relationship types: {schema['relationships']}\n"
        f"Known cities: {load_cities()}\nKnown tags: {load_tags()}\n"
        f"Attraction cost property: {COST_PROP} (rupees). Name property: {NAME_PROP}.\n"
        "Always add LIMIT 25. Return ONLY the Cypher, no markdown fences."
    )
    msg = client.messages.create(
        model=model, max_tokens=500, system=sys_prompt,
        messages=[{"role": "user", "content": question}],
    )
    cypher = msg.content[0].text.strip().strip("`").strip()
    if WRITE_WORDS.search(cypher):
        return "I can only run read-only queries."
    rows = run_query(cypher)
    if not rows:
        return "The query ran but returned no results."
    summary = client.messages.create(
        model=model, max_tokens=600,
        system="Answer the travel question concisely using only these database rows.",
        messages=[{"role": "user", "content": f"Question: {question}\nRows: {rows[:25]}"}],
    )
    return summary.content[0].text


def respond(question: str) -> str:
    try:
        return (
            answer_rule_based(question)
            or answer_with_llm(question)
            or "I didn't catch that. Try: *places in Mysore*, *free temples in Madurai*, "
               "*beaches under 100*, or *how to go from Chennai to Pondicherry*."
        )
    except Exception as exc:  # keep the UI alive on DB errors
        return f"Something went wrong talking to the database: `{exc}`"


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.set_page_config(page_title="South India Travel Chatbot", page_icon="🧭", layout="wide")
st.title("🧭 South India Travel Chatbot")
st.caption("Ask about cities, attractions, tags, costs and routes, answered from the Neo4j graph.")

with st.sidebar:
    st.header("Database")
    try:
        get_driver()
        st.success("Connected to Neo4j")
        schema = load_schema()
        st.write("**Labels:**", ", ".join(schema["labels"]))
        st.write("**Relationships:**", ", ".join(schema["relationships"]))
        st.write(f"**Cities:** {len(load_cities())}")
    except Exception as exc:
        st.error(f"Neo4j connection failed: {exc}")
        st.stop()
    st.header("Try asking")
    st.markdown("- Places in Mysore\n- Free temples in Madurai\n- Beaches under 100\n- Chennai to Pondicherry")
    if st.button("Clear chat"):
        st.session_state.pop("messages", None)
        st.rerun()

if "messages" not in st.session_state:
    st.session_state.messages = [
        {"role": "assistant", "content": "Hi! Ask me about places to visit across South India."}
    ]

for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

if prompt := st.chat_input("Ask about a city, place type, budget or route..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        with st.spinner("Searching the graph..."):
            reply = respond(prompt)
        st.markdown(reply)
    st.session_state.messages.append({"role": "assistant", "content": reply})
