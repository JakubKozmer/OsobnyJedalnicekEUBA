import json
import traceback
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from app import load_menu, filter_and_validate_plan


load_dotenv(Path(__file__).with_name(".env"))

try:
    menu = load_menu()
    print("Počet jedál načítaných z iOrders:", len(menu))

    profile = {
        "goal": "udržať si hmotnosť",
        "cuisines": ["slovenská a tradičná"],
        "proteins": ["kuracie mäso"],
        "cooking_methods": ["pečené", "na prírodno"],
        "sides": ["zemiaky", "ryža"],
        "soup_preference": "mám ju rád/rada",
        "vegetarian_days": "1-2",
        "spice_level": "mierne pikantné",
        "likes": "kuracie mäso, ryža",
        "dislikes": "",
        "allergens": [],
    }

    payload = {
        "profil_pouzivatela": profile,
        "aktualne_menu_iOrders": menu,
    }

    print("Odosielam menu agentovi…")

    client = OpenAI()
    agent_id = load_dotenv  # Len aby bolo vidno, že ID sa berie z .env
    import os
    agent_id = os.getenv("OPENAI_AGENT_ID")

    with client.beta.agents.sessions.create(
        agent_id=agent_id,
        environment={"type": "none"},
        input=json.dumps(payload, ensure_ascii=False),
        stream=True,
    ).with_result_collection() as stream:
        for event in stream:
            pass
        result = stream.get_final_result()

    print("Surová odpoveď agenta:")
    print(result.output_text)

    agent_json = json.loads(result.output_text)
    valid_plan = filter_and_validate_plan(agent_json, menu)

    print("Počet odporúčaní prijatých aplikáciou:", len(valid_plan))
    print(json.dumps(valid_plan, ensure_ascii=False, indent=2))

except Exception as exc:
    print("\nDIAGNOSTIKA ZLYHALA")
    print("Typ chyby:", type(exc).__name__)
    print("Chyba:", str(exc))
    traceback.print_exc()
