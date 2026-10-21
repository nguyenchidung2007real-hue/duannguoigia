"""
Bộ kiểm thử tự động chính thức cho Hệ thống Y tế AnTam AI (Pytest / Unittest).
Kiểm thử toàn diện 8 module cốt lõi:
1. Tiền xử lý văn bản giọng nói (hỗ trợ cả Web Speech API & PhoWhisper)
2. Kiểm tra giới hạn sinh lý & loại trừ lỗi số 0
3. Thang đo mức độ nghiêm trọng huyết áp (Severity 0 -> 4)
4. Giao thức cấp cứu 115 khi ở mức nguy kịch
5. Đánh giá lâm sàng nhịp tim (Tachycardia & Bradycardia)
6. Endpoint bóc tách giọng nói & hỏi lại khi thiếu số
7. Chốt chặn an toàn đơn thuốc (verified_by_caregiver)
8. Thẻ cứu hộ khẩn cấp Public QR
"""
import sys
import os
import unittest

# Đảm bảo import được backend từ thư mục tests
current_dir = os.path.dirname(os.path.abspath(__file__))
backend_dir = os.path.abspath(os.path.join(current_dir, ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from services.medical_guardrails import (
    evaluate_blood_pressure,
    preprocess_voice_text,
    normalize_bp_value,
    parse_vietnamese_number_in_text
)
from main import extract_bp_from_text, parse_voice_vitals, app, startup_seed_data
import schemas
from fastapi.testclient import TestClient

class TestAnTamMedicalGuardrails(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        startup_seed_data()
        cls.client = TestClient(app)

    # 1. TEST TIỀN XỬ LÝ VĂN BẢN & PHÂN TÁCH SỐ (BAO GỒM PHOBISPER)
    def test_voice_text_preprocessing(self):
        cases = [
            ("huyết áp một trăm ba mươi lăm trên tám mươi lăm", (135, 85, None)),
            ("một trăm bốn mươi trên chín mươi", (140, 90, None)),
            ("huyết áp 12 5 nhịp tim 70", (125, None, 70.0)),
            ("huyết áp 120 nhịp tim 70", (120, None, 70.0)),
            ("huyết áp 120", (120, None, None)),
            ("13 rưỡi", (135, None, None)),
            ("tâm trương 80", (None, 80, None)),
            ("135 trên 85", (135, 85, None)),
            ("13 rưỡi trên 8 rưỡi", (135, 85, None)),
            ("13 8", (130, 80, None)),
            ("12 7", (120, 70, None)),
            ("13 rưỡi 8 rưỡi", (135, 85, None)),
            ("12 5 8 5", (125, 85, None)),
            ("12 5 80", (125, 80, None)),
            ("120 80 75", (120, 80, 75.0)),
            ("13 8 72", (130, 80, 72.0)),
            ("mười ba rưỡi trên tám rưỡi", (135, 85, None)),
            ("13 phẩy 5 trên 8 phẩy 5", (135, 85, None)),
            ("13 phẩy năm trên 8 phẩy năm", (135, 85, None)),
            ("1 2 0 trên 80", (120, 80, None)),
            ("1 2 0 trên 8 0", (120, 80, None)),
            ("155 trên 55", (155, 55, None)),
            ("180 trên 110", (180, 110, None)),
            ("80 trên 130", (80, 130, None))
        ]
        for raw, expected in cases:
            res = extract_bp_from_text(raw)
            self.assertEqual(res, expected, f"Failed on text: '{raw}' -> got {res}, expected {expected}")

    # 2. TEST GIỚI HẠN SINH LÝ & SỐ 0
    def test_physiological_bounds_and_zero(self):
        invalid_cases = [
            (0, 80, None),    # Số 0 không hợp lệ
            (270, 90, None),  # Tâm thu > 260
            (55, 40, None),   # Tâm thu < 60
            (120, 25, None),  # Tâm trương < 30
            (120, 170, None), # Tâm trương > 160
            (120, 80, 250),   # Nhịp tim > 220
            (120, 80, 20)     # Nhịp tim < 30
        ]
        for s, d, p in invalid_cases:
            res = evaluate_blood_pressure(s, d, p)
            self.assertEqual(res["category"], "INVALID", f"Expected INVALID for {s}/{d} (pulse={p})")
            self.assertEqual(res["severity_level"], 0)

    # 3. TEST PHÂN TẦNG SEVERITY & DANH MỤC Y KHOA
    def test_categories_and_severity_scores(self):
        checks = [
            (115, 75, "NORMAL", 0),
            (130, 85, "HIGH_NORMAL", 1),
            (145, 92, "HYPERTENSION_STAGE_1", 2),
            (165, 102, "HYPERTENSION_STAGE_2", 3),
            (155, 55, "MIXED_DANGER", 3),
            (89, 60, "HYPOTENSION_MILD", 1),
            (85, 55, "HYPOTENSION_MILD", 1),
            (75, 45, "HYPOTENSION_LOW", 3),
            (185, 115, "CRITICAL_HIGH", 4),
            (65, 38, "CRITICAL_LOW", 4),
            (80, 120, "INVALID", 0)
        ]
        for s, d, exp_cat, exp_sev in checks:
            res = evaluate_blood_pressure(s, d)
            self.assertEqual(res["category"], exp_cat, f"Category mismatch for {s}/{d}")
            self.assertEqual(res["severity_level"], exp_sev, f"Severity mismatch for {s}/{d}")

    # 4. TEST GIAO THỨC CẤP CỨU 115 BẮT BUỘC Ở SEVERITY 4
    def test_emergency_115_protocol(self):
        res_high = evaluate_blood_pressure(185, 115)
        res_low = evaluate_blood_pressure(65, 38)
        self.assertTrue(res_high["is_emergency"])
        self.assertTrue(res_low["is_emergency"])
        self.assertIn("115", res_high["advice"])
        self.assertIn("115", res_low["advice"])

    # 5. TEST ĐÁNH GIÁ LÂM SÀNG NHỊP TIM
    def test_pulse_clinical_evaluation(self):
        pulse_checks = [
            (120, 80, 110, "TACHYCARDIA"),
            (120, 80, 130, "TACHYCARDIA_HIGH"),
            (120, 80, 55, "BRADYCARDIA"),
            (120, 80, 45, "BRADYCARDIA_LOW"),
            (120, 80, 75, "NORMAL")
        ]
        for s, d, p, exp_status in pulse_checks:
            res = evaluate_blood_pressure(s, d, p)
            self.assertEqual(res.get("pulse_status"), exp_status)

    # 6. TEST API BÓC TÁCH GIỌNG NÓI & HỎI LẠI KHI THIẾU SỐ
    def test_parse_voice_vitals_endpoint(self):
        # Trường hợp 1: Chỉ có 1 số -> hỏi lại
        inp_single = schemas.VitalSignParseInput(elderly_id="lan_1952", raw_transcript="huyết áp 12 5 nhịp tim 70")
        out_single = parse_voice_vitals(inp_single)
        self.assertFalse(out_single["success"])
        self.assertTrue(out_single["need_retry"])
        self.assertIn("125", out_single["message"])

        # Trường hợp 2: Không có số nào -> từ chối
        inp_none = schemas.VitalSignParseInput(elderly_id="lan_1952", raw_transcript="không có số nào ở đây")
        out_none = parse_voice_vitals(inp_none)
        self.assertFalse(out_none["success"])
        self.assertTrue(out_none["need_retry"])

        # Trường hợp 3: Hợp lệ -> chờ xác nhận 2 bước
        inp_valid = schemas.VitalSignParseInput(elderly_id="lan_1952", raw_transcript="135 trên 85")
        out_valid = parse_voice_vitals(inp_valid)
        self.assertTrue(out_valid["success"])
        self.assertTrue(out_valid["need_confirmation"])
        self.assertEqual(out_valid["systolic"], 135)
        self.assertEqual(out_valid["diastolic"], 85)

    # 7. TEST CHỐNG LỖ HỔNG ĐƠN THUỐC CHƯA DUYỆT (verified_by_caregiver)
    def test_caregiver_verification_safety(self):
        res = self.client.get("/api/elderly/lan_1952/medications/today")
        self.assertEqual(res.status_code, 200)
        meds = res.json()
        self.assertTrue(len(meds) >= 2)
        for m in meds:
            self.assertIn(m["medicine_name"], ["Amlodipine 5mg", "Glucophage 500mg"])

    # 8. TEST THẺ CỨU HỘ PUBLIC QR & NÚT SOS
    def test_emergency_qr_and_sos(self):
        res_qr = self.client.get("/qr/qr_lan_1952")
        self.assertEqual(res_qr.status_code, 200)
        self.assertIn("Bà Nguyễn Thị Lan", res_qr.text)
        self.assertIn("0912345678", res_qr.text)
        self.assertIn("GỌI CẤP CỨU 115", res_qr.text)

        res_sos = self.client.post("/api/elderly/lan_1952/sos")
        self.assertEqual(res_sos.status_code, 200)
        self.assertTrue(res_sos.json()["success"])

if __name__ == "__main__":
    unittest.main()
