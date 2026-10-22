import sys
sys.stdout.reconfigure(encoding='utf-8')
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_conversation_log_flow():
    # 1. Log 1 turn hội thoại CORRECT (SOS intent)
    res = client.post("/api/conversation/log", json={
        "elderly_id": "lan_1952",
        "user_input": "tự nhiên đau ngực quá con ơi",
        "input_source": "VOICE",
        "detected_intent": "SOS",
        "intent_confidence": 0.97,
        "ai_response": "Dạ thưa cụ, cụ đang cảm thấy đau ngực ạ! Con đang báo động cho gia đình ngay. Cụ nằm nghỉ tại chỗ, không đi lại nhé ạ!",
        "response_source": "RULE_BASED",
        "session_id": "sess_test_001"
    })
    assert res.status_code == 200
    log_id = res.json()["id"]
    assert res.json()["detected_intent"] == "SOS"
    print("1. ConversationLog SOS turn created:", log_id)

    # 2. Log 1 turn CHITCHAT
    res2 = client.post("/api/conversation/log", json={
        "elderly_id": "lan_1952",
        "user_input": "hôm nay trời đẹp quá",
        "input_source": "VOICE",
        "detected_intent": "CHITCHAT",
        "intent_confidence": 0.85,
        "ai_response": "Dạ trời hôm nay đẹp thật ạ! Cụ có muốn con kể chuyện hay nghe nhạc không ạ?",
        "response_source": "RULE_BASED",
        "session_id": "sess_test_001"
    })
    assert res2.status_code == 200
    log_id2 = res2.json()["id"]
    print("2. ConversationLog CHITCHAT turn created:", log_id2)

    # 3. Caregiver feedback: đánh dấu CORRECT
    res_fb = client.patch(f"/api/conversation/log/{log_id}/feedback", json={
        "caregiver_feedback": "CORRECT"
    })
    assert res_fb.status_code == 200
    assert res_fb.json()["success"] == True
    print("3. Caregiver feedback CORRECT applied")

    # 4. Caregiver sửa WRONG response
    res_fb2 = client.patch(f"/api/conversation/log/{log_id2}/feedback", json={
        "caregiver_feedback": "WRONG",
        "corrected_response": "Dạ thưa cụ, trời đẹp thật ạ! Cụ nhớ uống đủ nước và không ra nắng quá lâu nhé ạ!"
    })
    assert res_fb2.status_code == 200
    print("4. Caregiver feedback WRONG + correction applied")

    # 5. Export dataset — chỉ CORRECT rows
    res_export = client.get("/api/admin/export-dataset?only_verified=true&elderly_id=lan_1952")
    assert res_export.status_code == 200
    data = res_export.json()
    assert data["llm_dataset_rows"] >= 1
    assert "messages" in data["llm_jsonl"]
    assert data["clf_dataset_rows"] >= 1
    assert "SOS" in data["clf_jsonl"]
    print("5. Export dataset verified:", data["llm_dataset_rows"], "LLM rows,", data["clf_dataset_rows"], "CLF rows")

    # 6. Export all (kể cả WRONG với corrected_response)
    res_all = client.get("/api/admin/export-dataset?only_verified=false&elderly_id=lan_1952")
    assert res_all.json()["llm_dataset_rows"] >= 2  # cả CORRECT lẫn WRONG+correction
    print("6. Export all rows (incl. corrections):", res_all.json()["llm_dataset_rows"])

    print("ALL_CONVERSATION_LOG_TESTS_PASSED_100%")

if __name__ == "__main__":
    test_conversation_log_flow()
