"""The interactive setup guide: quests are ticked off by the account's real data, never by the guide itself."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import Evidence, KnowledgeAnswer, SavedSearch, User
from app.services import setup_guide


@pytest.fixture
def client(anonymous):
    c = TestClient(app)
    email = f"guide-{uuid.uuid4().hex[:10]}@example.com"
    assert c.post("/api/auth/signup", json={"email": email, "password": "password for guide",
                                            "confirm_password": "password for guide"}).status_code == 201
    with SessionLocal() as db:
        c.user_id = db.scalar(select(User.id).where(User.email == email))
    return c


def quest(guide, quest_id):
    return next(q for q in guide["quests"] if q["id"] == quest_id)


def test_a_new_account_starts_at_level_one_with_nothing_done(client):
    g = client.get("/api/setup/guide").json()
    assert g["done"] == 0 and g["xp"] == 0 and not g["complete"]
    assert g["total"] == len([q for q in setup_guide.QUESTS if not q.optional])
    assert g["level"]["number"] == 1 and g["level"]["title"] == "Rookie"
    assert g["next"] == "profile"
    assert g["dismissed"] is False and g["welcomed"] is False and g["celebrated"] == []
    # The test server runs the mock model: that is not a connected AI.
    assert not quest(g, "ai")["done"] and "mock" in quest(g, "ai")["detail"]


def test_quests_follow_the_accounts_own_data(client):
    client.put("/api/profile", json={"name": "Ada", "headline": "Backend engineer", "target_roles": ["Lead"],
                                     "preferred_locations": ["Remote"], "master_resume": "Ten years of Python"})
    with SessionLocal() as db:
        db.add_all([Evidence(user_id=client.user_id, claim="Shipped payments", verified=True),
                    KnowledgeAnswer(user_id=client.user_id, question="Notice period?", question_key="notice",
                                    answer="30 days", verified=True),
                    SavedSearch(user_id=client.user_id, name="Backend", settings={})])
        db.commit()
    g = client.get("/api/setup/guide").json()
    done = {q["id"] for q in g["quests"] if q["done"]}
    assert done == {"profile", "preferences", "resume", "verify", "answers", "search"}
    assert g["xp"] == sum(q["xp"] for q in g["quests"] if q["done"])
    assert g["level"]["number"] > 1
    assert g["next"] == "ai"


def test_another_accounts_data_does_not_count(client):
    with SessionLocal() as db:
        other = User(email=f"other-{uuid.uuid4().hex[:8]}@example.com", password_hash="!")
        db.add(other)
        db.flush()
        db.add(SavedSearch(user_id=other.id, name="Theirs", settings={}))
        db.commit()
    assert not quest(client.get("/api/setup/guide").json(), "search")["done"]


def test_guide_state_is_saved_and_celebrations_only_accumulate(client):
    g = client.put("/api/setup/guide", json={"dismissed": True, "celebrated": ["profile", "not-a-quest"]}).json()
    assert g["dismissed"] is True and g["celebrated"] == ["profile"]
    g = client.put("/api/setup/guide", json={"celebrated": ["resume"]}).json()
    assert g["celebrated"] == ["profile", "resume"] and g["dismissed"] is True
    g = client.put("/api/setup/guide", json={"dismissed": False, "welcomed": True}).json()
    assert g["dismissed"] is False and g["welcomed"] is True and g["celebrated"] == ["profile", "resume"]


def test_gmail_is_an_optional_bonus_that_never_blocks_completion(client):
    gmail = quest(client.get("/api/setup/guide").json(), "gmail")
    assert gmail["optional"] and not gmail["done"]
    assert "not configured" in gmail["detail"]


@pytest.mark.parametrize("xp,number,title", [(0, 1, "Rookie"), (149, 1, "Rookie"), (150, 2, "Job Seeker"),
                                             (10_000, 5, "Offer Magnet")])
def test_levels(xp, number, title):
    level = setup_guide.level_for(xp)
    assert (level["number"], level["title"]) == (number, title)
    assert (level["next"] is None) == (number == len(setup_guide.LEVELS))


def test_every_quest_fits_in_the_xp_ladder():
    assert sum(q.xp for q in setup_guide.QUESTS) >= setup_guide.LEVELS[-1][0]


def test_the_ai_quest_recommends_ollama_cloud_through_litellm(client):
    ai = quest(client.get("/api/setup/guide").json(), "ai")
    assert ai["screen"] == "orchestration" and ai["section"] == "models"  # AI models live in AI Orchestration
    assert "nemotron-3-nano:30b-cloud" in ai["recommended"] and "LiteLLM" in ai["recommended"]
    assert "integrations/litellm/config.ollama-cloud.yaml" in ai["snippet"]
    assert any(link["url"] == "https://ollama.com/settings/keys" for link in ai["links"])


def test_the_ollama_cloud_config_keeps_credentials_in_the_environment():
    from pathlib import Path

    import yaml

    config = yaml.safe_load(Path("integrations/litellm/config.ollama-cloud.yaml").read_text())
    [model] = config["model_list"]
    assert model["model_name"] == "nemotron-3-nano:30b-cloud"
    assert model["litellm_params"] == {"model": "ollama_chat/nemotron-3-nano:30b", "api_base": "https://ollama.com",
                                       "api_key": "os.environ/OLLAMA_API_KEY"}
    assert config["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"
