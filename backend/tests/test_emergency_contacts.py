import sys
sys.stdout.reconfigure(encoding='utf-8')
from fastapi.testclient import TestClient
from main import app
from database import SessionLocal
import models

client = TestClient(app)

def test_emergency_contacts_flow():
    # 1. Get contacts for lan_1952 (should contain primary contact Nguyen Van Hung)
    res = client.get("/api/family/lan_1952/contacts")
    assert res.status_code == 200
    contacts = res.json()
    assert len(contacts) >= 1
    primary = contacts[0]
    assert primary["is_primary_contact"] == True
    assert "0912345678" in primary["phone_number"]
    print("1. Primary contact verified:", primary["full_name"], primary["phone_number"])

    # 2. Add invalid phone number -> must fail with 422
    res_invalid = client.post(
        "/api/family/lan_1952/contacts",
        json={
            "full_name": "Test Người Lạ",
            "phone_number": "12345",
            "relationship_type": "CON_GAI",
            "is_primary_contact": False
        }
    )
    assert res_invalid.status_code == 422
    print("2. Invalid phone validation verified (422 rejected)")

    # 3. Add valid secondary contact
    res_add = client.post(
        "/api/family/lan_1952/contacts",
        json={
            "full_name": "Nguyễn Thị Mai",
            "phone_number": "0987654321",
            "relationship_type": "CON_GAI",
            "is_primary_contact": False
        }
    )
    assert res_add.status_code == 200
    new_c = res_add.json()
    assert new_c["full_name"] == "Nguyễn Thị Mai"
    new_id = new_c["id"]
    print("3. Secondary contact created:", new_c["full_name"], new_c["phone_number"])

    # 4. Check list has 2 contacts
    res_list2 = client.get("/api/family/lan_1952/contacts")
    assert len(res_list2.json()) >= 2
    print("4. Contact count increased to 2")

    # 5. Cannot delete primary contact -> must fail with 400
    res_del_prim = client.delete(f"/api/family/lan_1952/contacts/{primary['id']}")
    assert res_del_prim.status_code == 400
    print("5. Protected primary contact from deletion verified (400 rejected)")

    # 6. IDOR protection: cannot delete contact under wrong elderly_id -> 404
    res_idor = client.delete(f"/api/family/lan_other/contacts/{new_id}")
    assert res_idor.status_code == 404
    print("6. IDOR protection verified (404 for wrong elderly_id)")

    # 7. Delete secondary contact -> must succeed
    res_del_sec = client.delete(f"/api/family/lan_1952/contacts/{new_id}")
    assert res_del_sec.status_code == 200
    print("7. Secondary contact deleted successfully")

    # 8. Final verify
    res_final = client.get("/api/family/lan_1952/contacts")
    assert not any(c["id"] == new_id for c in res_final.json())
    print("ALL_EMERGENCY_CONTACTS_TESTS_PASSED_100%")

if __name__ == "__main__":
    test_emergency_contacts_flow()
