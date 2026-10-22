import sys
sys.stdout.reconfigure(encoding='utf-8')
from fastapi.testclient import TestClient
from main import app
from database import SessionLocal
import models

client = TestClient(app)

def test_prescription_ocr_flow():
    # 1. Test OCR text parsing with realistic Vietnamese prescription
    sample_prescription = """
    ĐƠN THUỐC NGOẠI TRÚ
    Bệnh nhân: Nguyễn Thị Lan (74 tuổi)
    Chẩn đoán: Tăng huyết áp, Đái tháo đường

    1. Amlodipine 5mg: 1 viên, uống sáng sau ăn (08:00)
    2. Metformin 850mg: 1 viên x 2 lần/ngày (sáng 08:00, tối 19:30)
    3. Atorvastatin 20mg: 1 viên, uống buổi tối trước khi đi ngủ (20:00)
    """

    res_ocr = client.post(
        "/api/family/lan_1952/prescriptions/ocr",
        data={"raw_text": sample_prescription}
    )
    assert res_ocr.status_code == 200
    data = res_ocr.json()
    assert data["success"] == True
    assert data["verified_by_caregiver"] == False  # Medical safety: unverified until caregiver reviews!
    assert "⚠️ LƯU Ý Y TẾ" in data["warning"]
    
    meds = data["extracted_medicines"]
    assert len(meds) == 3
    names = [m["medicine_name"] for m in meds]
    assert any("Amlodipine" in n for n in names)
    assert any("Metformin" in n for n in names)
    assert any("Atorvastatin" in n for n in names)
    print("1. Heuristic OCR parsed 3 medications with medical safety banner:", names)

    # 2. Test IDOR protection: non-existent elderly_id returns 404
    res_fake = client.post(
        "/api/family/lan_non_existent/prescriptions/ocr",
        data={"raw_text": sample_prescription}
    )
    assert res_fake.status_code == 404
    print("2. IDOR protection verified for OCR endpoint (404 for invalid elderly)")

    # 3. Test Caregiver Batch-create (Review & Commit to Schedule)
    batch_payload = {
        "raw_ocr_text": sample_prescription.strip(),
        "medications": [
            {
                "elderly_id": "lan_1952",
                "medicine_name": "Thuốc Thử Nghiệm OCR Amlodipine",
                "dosage": "1 viên",
                "instructions": "Uống sau ăn sáng",
                "schedule_times": ["08:00"],
                "pill_color_shape": "Viên vàng tròn"
            },
            {
                "elderly_id": "lan_1952",
                "medicine_name": "Thuốc Thử Nghiệm OCR Metformin",
                "dosage": "1 viên",
                "instructions": "Uống sau ăn tối",
                "schedule_times": ["19:30"],
                "pill_color_shape": "Viên trắng dài"
            }
        ]
    }

    res_batch = client.post(
        "/api/family/lan_1952/prescriptions/batch-create",
        json=batch_payload
    )
    assert res_batch.status_code == 200
    batch_res = res_batch.json()
    assert batch_res["success"] == True
    assert batch_res["count"] == 2
    for m in batch_res["medications"]:
        assert m["verified_by_caregiver"] == True  # Caregiver reviewed and approved!
    print("3. Batch create confirmed & verified by caregiver:", batch_res["count"], "meds created")

    # 4. Verify in caregiver medication list
    res_list = client.get("/api/family/lan_1952/medications")
    assert res_list.status_code == 200
    caregiver_meds = res_list.json()
    created_names = [m["medicine_name"] for m in caregiver_meds]
    assert "Thuốc Thử Nghiệm OCR Amlodipine" in created_names
    assert "Thuốc Thử Nghiệm OCR Metformin" in created_names
    print("4. New medications successfully listed in family medication dashboard")

    # 5. Clean up created test medications
    db = SessionLocal()
    db.query(models.MedicationSchedule).filter(
        models.MedicationSchedule.medicine_name.like("Thuốc Thử Nghiệm OCR%")
    ).delete(synchronize_session=False)
    db.commit()
    db.close()
    print("5. Test cleanup completed")
    print("ALL_PRESCRIPTION_OCR_TESTS_PASSED_100%")

if __name__ == "__main__":
    test_prescription_ocr_flow()
