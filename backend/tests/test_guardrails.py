"""
Bộ kiểm thử tự động toàn diện cho Hệ thống Y tế AnTam AI (Pytest / Unittest).
Kiểm thử 10 module cốt lõi & các chốt chặn an toàn y khoa:
1. Tiền xử lý văn bản giọng nói (hỗ trợ cả Web Speech API & PhoWhisper)
2. Kiểm tra giới hạn sinh lý & loại trừ lỗi số 0
3. Thang đo mức độ nghiêm trọng huyết áp (Severity 0 -> 4) & phân tầng tụt huyết áp
4. Giao thức cấp cứu 115 khi ở mức nguy kịch (CRITICAL_HIGH & CRITICAL_LOW)
5. Đánh giá lâm sàng nhịp tim (Tachycardia & Bradycardia)
6. Kiểm tra múi giờ chuẩn Việt Nam (get_vn_now là datetime naive)
7. Endpoint bóc tách giọng nói & hỏi lại khi thiếu số
8. Chốt chặn confirm-log từ chối INVALID và chống spam SOS trùng lặp
9. Thẻ cứu hộ Public QR (Read-only, chống XSS & gửi định vị GPS)
10. Bộ lập lịch thuốc 2 lượt (Pass 1 khởi tạo cữ thuốc, Pass 2 leo thang nhắc nhở & Deadman alert)
11. Quy trình quản lý và xác thực đơn thuốc của người nhà (Caregiver verification)
12. Chống tấn công Brute-force mã ghép nối 6 chữ số
"""
from datetime import datetime, timedelta
import os
import re
import sys
import unittest

# Đảm bảo import được backend từ thư mục tests
current_dir = os.path.dirname(os.path.abspath(__file__))
backend_dir = os.path.abspath(os.path.join(current_dir, ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
import main
from main import (
    FAILED_PAIR_ATTEMPTS,
    app,
    check_medication_schedules,
    extract_bp_from_text,
    parse_voice_vitals,
)
import models
import schemas
from services.medical_guardrails import (
    evaluate_blood_pressure,
    normalize_bp_value,
    parse_vietnamese_number_in_text,
    preprocess_voice_text,
)

# Sử dụng in-memory database với StaticPool để giữ nguyên bảng qua mọi thread và session
TEST_DATABASE_URL = "sqlite:///:memory:"
test_engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=test_engine
)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


class TestAnTamMedicalGuardrails(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=test_engine)
        app.dependency_overrides[get_db] = override_get_db
        # Gán SessionLocal của main sang TestingSessionLocal để check_medication_schedules chạy trên test DB
        main.SessionLocal = TestingSessionLocal
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(bind=test_engine)
        app.dependency_overrides.clear()

    def setUp(self):
        # Dọn dẹp dữ liệu và reset state trước mỗi test
        db = TestingSessionLocal()
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())
        db.commit()

        # Seed profile mẫu
        profile = models.ElderlyProfile(
            id="lan_1952",
            full_name="Bà Nguyễn Thị Lan",
            birth_year=1952,
            gender="FEMALE",
            address="Số 45, Phố Huế, Hà Nội",
            medical_history="Tăng huyết áp vô căn",
            drug_allergies="Dị ứng Penicillin",
            pairing_code="123456",
            emergency_qr_token="qr_lan_1952",
        )
        contact = models.FamilyMember(
            id="contact_1",
            elderly_id="lan_1952",
            full_name="Nguyễn Văn Hùng",
            phone_number="0912345678",
            relationship_type="CON_TRAI",
            is_primary_contact=True,
        )
        med = models.MedicationSchedule(
            id="med_1",
            elderly_id="lan_1952",
            medicine_name="Amlodipine 5mg",
            pill_color_shape="Viên tròn nhỏ màu vàng",
            dosage="1 viên",
            instructions="Uống sau ăn sáng",
            schedule_times=["08:00"],
            verified_by_caregiver=True,
        )
        db.add_all([profile, contact, med])
        db.commit()
        db.close()
        FAILED_PAIR_ATTEMPTS.clear()

    # 1. TEST TIỀN XỬ LÝ VĂN BẢN & PHÂN TÁCH SỐ (BAO GỒM PHOBISPER)
    def test_voice_text_preprocessing(self):
        cases = [
            (
                "huyết áp một trăm ba mươi lăm trên tám mươi lăm",
                (135, 85, None),
            ),
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
            ("80 trên 130", (80, 130, None)),
        ]
        for raw, expected in cases:
            res = extract_bp_from_text(raw)
            self.assertEqual(
                res,
                expected,
                f"Failed on text: '{raw}' -> got {res}, expected {expected}",
            )

    # 2. TEST GIỚI HẠN SINH LÝ & SỐ 0
    def test_physiological_bounds_and_zero(self):
        invalid_cases = [
            (0, 80, None),  # Số 0 không hợp lệ
            (270, 90, None),  # Tâm thu > 260
            (55, 40, None),  # Tâm thu < 60
            (120, 25, None),  # Tâm trương < 30
            (120, 170, None),  # Tâm trương > 160
            (120, 80, 250),  # Nhịp tim > 220
            (120, 80, 20),  # Nhịp tim < 30
        ]
        for s, d, p in invalid_cases:
            res = evaluate_blood_pressure(s, d, p)
            self.assertEqual(
                res["category"],
                "INVALID",
                f"Expected INVALID for {s}/{d} (pulse={p})",
            )
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
            (80, 120, "INVALID", 0),
        ]
        for s, d, exp_cat, exp_sev in checks:
            res = evaluate_blood_pressure(s, d)
            self.assertEqual(
                res["category"], exp_cat, f"Category mismatch for {s}/{d}"
            )
            self.assertEqual(
                res["severity_level"], exp_sev, f"Severity mismatch for {s}/{d}"
            )

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
            (120, 80, 75, "NORMAL"),
        ]
        for s, d, p, exp_status in pulse_checks:
            res = evaluate_blood_pressure(s, d, p)
            self.assertEqual(res.get("pulse_status"), exp_status)

    # 6. TEST MÚI GIỜ CHUẨN VIỆT NAM (get_vn_now LÀ DATETIME NAIVE)
    def test_get_vn_now_is_naive(self):
        vn_time = models.get_vn_now()
        self.assertIsInstance(vn_time, datetime)
        self.assertIsNone(
            vn_time.tzinfo, "get_vn_now phải trả về naive datetime để SQLite so sánh chuẩn xác"
        )

    # 7. TEST API BÓC TÁCH GIỌNG NÓI & HỎI LẠI KHI THIẾU SỐ
    def test_parse_voice_vitals_endpoint(self):
        inp_single = schemas.VitalSignParseInput(
            elderly_id="lan_1952", raw_transcript="huyết áp 12 5 nhịp tim 70"
        )
        out_single = parse_voice_vitals(inp_single)
        self.assertFalse(out_single["success"])
        self.assertTrue(out_single["need_retry"])
        self.assertIn("125", out_single["message"])

        inp_none = schemas.VitalSignParseInput(
            elderly_id="lan_1952", raw_transcript="không có số nào ở đây"
        )
        out_none = parse_voice_vitals(inp_none)
        self.assertFalse(out_none["success"])
        self.assertTrue(out_none["need_retry"])

        inp_valid = schemas.VitalSignParseInput(
            elderly_id="lan_1952", raw_transcript="135 trên 85"
        )
        out_valid = parse_voice_vitals(inp_valid)
        self.assertTrue(out_valid["success"])
        self.assertTrue(out_valid["need_confirmation"])
        self.assertEqual(out_valid["systolic"], 135)
        self.assertEqual(out_valid["diastolic"], 85)

    # 8. TEST CHỐT CHẶN CONFIRM-LOG TỪ CHỐI INVALID VÀ CHỐNG TRÙNG LẶP SOS
    def test_confirm_log_invalid_rejection(self):
        # Trường hợp số ngược: 80 / 120 -> phải trả về lỗi HTTP 400
        payload = {
            "elderly_id": "lan_1952",
            "systolic": 80.0,
            "diastolic": 120.0,
            "heart_rate": 75.0,
            "raw_transcript": "80 trên 120",
        }
        res = self.client.post("/api/elderly/vitals/confirm-log", json=payload)
        self.assertEqual(res.status_code, 400)
        self.assertIn("tâm thu", res.json()["detail"].lower())

    def test_confirm_log_vital_critical_and_deduplication(self):
        db = TestingSessionLocal()
        payload = {
            "elderly_id": "lan_1952",
            "systolic": 190.0,
            "diastolic": 115.0,
            "heart_rate": 80.0,
            "raw_transcript": "190 trên 115",
        }
        res1 = self.client.post("/api/elderly/vitals/confirm-log", json=payload)
        self.assertEqual(res1.status_code, 200)

        # Kiểm tra đã ghi log SOS nguy kịch
        sos_count_1 = (
            db.query(models.HealthAndSOSLog)
            .filter(
                models.HealthAndSOSLog.elderly_id == "lan_1952",
                models.HealthAndSOSLog.trigger_source == "VITAL_CRITICAL",
            )
            .count()
        )
        self.assertEqual(sos_count_1, 1)

        # Gửi tiếp lần 2 trong vòng 10 phút -> không được tạo trùng SOS
        res2 = self.client.post("/api/elderly/vitals/confirm-log", json=payload)
        self.assertEqual(res2.status_code, 200)
        sos_count_2 = (
            db.query(models.HealthAndSOSLog)
            .filter(
                models.HealthAndSOSLog.elderly_id == "lan_1952",
                models.HealthAndSOSLog.trigger_source == "VITAL_CRITICAL",
            )
            .count()
        )
        self.assertEqual(sos_count_2, 1, "SOS nguy kịch phải được deduplicate trong 10 phút")
        db.close()

    # 9. TEST THẺ CỨU HỘ PUBLIC QR (READ-ONLY, CHỐNG XSS & ALERT CÓ GPS)
    def test_emergency_qr_readonly_and_xss_protection(self):
        db = TestingSessionLocal()
        # Tạo profile có chứa payload XSS độc hại
        xss_profile = models.ElderlyProfile(
            id="xss_profile",
            full_name="<script>alert('xss')</script>Cụ Thọ",
            birth_year=1940,
            address="<img src=x onerror=alert(1)>",
            medical_history="<svg onload=alert(2)>Tiểu đường",
            drug_allergies="<iframe src=javascript:alert(3)>Aspirin",
            pairing_code="654321",
            emergency_qr_token="qr_xss_token",
        )
        db.add(xss_profile)
        db.commit()

        initial_sos_count = db.query(models.HealthAndSOSLog).count()

        # GET request tới /qr/{token}
        res = self.client.get("/qr/qr_xss_token")
        self.assertEqual(res.status_code, 200)

        # 1. Chống XSS: Các thẻ html độc hại từ dữ liệu người dùng phải được escape
        self.assertNotIn("<script>alert('xss')</script>", res.text)
        self.assertNotIn("<img src=x onerror=alert(1)>", res.text)
        self.assertNotIn("<svg onload=alert(2)>", res.text)
        self.assertNotIn("<iframe src=javascript:alert(3)>", res.text)
        self.assertIn("&lt;script&gt;alert", res.text)
        self.assertIn("&lt;img src=x", res.text)
        self.assertIn("&lt;svg onload=alert(2)&gt;", res.text)
        self.assertIn("&lt;iframe src=javascript:alert(3)&gt;", res.text)

        # 2. Read-only: GET không được phép tạo thêm bản ghi SOS
        after_get_sos_count = db.query(models.HealthAndSOSLog).count()
        self.assertEqual(
            initial_sos_count,
            after_get_sos_count,
            "GET /qr/{token} phải là Read-only, không được kích hoạt SOS!",
        )

        # 3. Kích hoạt POST /qr/{token}/alert kèm GPS
        alert_payload = {
            "latitude": 21.028511,
            "longitude": 105.854212,
            "helper_note": "Cụ đang ngồi ở bến xe buýt",
        }
        res_alert = self.client.post("/qr/qr_xss_token/alert", json=alert_payload)
        self.assertEqual(res_alert.status_code, 200)
        self.assertTrue(res_alert.json()["success"])

        # Xác nhận SOS đã được tạo sau POST chủ động
        new_sos = (
            db.query(models.HealthAndSOSLog)
            .filter(
                models.HealthAndSOSLog.elderly_id == "xss_profile",
                models.HealthAndSOSLog.trigger_source == "PUBLIC_QR_SCAN",
            )
            .first()
        )
        self.assertIsNotNone(new_sos)
        self.assertIn("GPS: 21.02851, 105.85421", new_sos.location_note)
        db.close()

    # 10. TEST BỘ LẬP LỊCH THUỐC 2 LƯỢT (PASS 1 KHỞI TẠO, PASS 2 LEO THANG & DEADMAN ALERT)
    def test_medication_scheduler_pass1_and_pass2_escalation(self):
        import asyncio

        db = TestingSessionLocal()
        med = db.query(models.MedicationSchedule).filter(models.MedicationSchedule.id == "med_1").first()

        now = models.get_vn_now()
        med.schedule_times = [now.strftime("%H:%M")]
        db.commit()

        # PASS 1: Quét lịch thuốc tại phút hiện tại -> Phải tạo REMINDED_1
        asyncio.run(check_medication_schedules())

        log = (
            db.query(models.MedicationLog)
            .filter(models.MedicationLog.medication_id == "med_1")
            .first()
        )
        self.assertIsNotNone(log)
        self.assertEqual(log.status, "REMINDED_1")
        self.assertEqual(log.reminder_count, 1)

        # PASS 2 - Lần nhắc 2: Tua thời gian giả định qua 6 phút (diff_mins >= 5)
        log.scheduled_time = now - timedelta(minutes=6)
        db.commit()
        asyncio.run(check_medication_schedules())
        db.refresh(log)
        self.assertEqual(log.status, "REMINDED_2")
        self.assertEqual(log.reminder_count, 2)

        # PASS 2 - Lần nhắc 3: Tua thời gian giả định qua 11 phút (diff_mins >= 10)
        log.scheduled_time = now - timedelta(minutes=11)
        db.commit()
        asyncio.run(check_medication_schedules())
        db.refresh(log)
        self.assertEqual(log.status, "REMINDED_3")
        self.assertEqual(log.reminder_count, 3)

        # PASS 2 - Leo thang Bỏ lỡ thuốc: Tua thời gian giả định qua 22 phút (diff_mins >= 20)
        log.scheduled_time = now - timedelta(minutes=22)
        db.commit()
        asyncio.run(check_medication_schedules())
        db.refresh(log)
        self.assertEqual(log.status, "MISSED_ESCALATED")

        # Kiểm tra đã tạo bản ghi DEADMAN_MISSED trong HealthAndSOSLog
        deadman_log = (
            db.query(models.HealthAndSOSLog)
            .filter(
                models.HealthAndSOSLog.elderly_id == "lan_1952",
                models.HealthAndSOSLog.trigger_source == "DEADMAN_MISSED",
            )
            .first()
        )
        self.assertIsNotNone(deadman_log)
        self.assertIn("Amlodipine 5mg", deadman_log.location_note)
        db.close()

    # 11. TEST QUY TRÌNH XÁC THỰC ĐƠN THUỐC CỦA NGƯỜI NHÀ (CAREGIVER WORKFLOW)
    def test_caregiver_medication_create_and_verify(self):
        db = TestingSessionLocal()
        # 1. Tạo đơn thuốc chưa xác thực (ví dụ import từ nguồn bên ngoài)
        unverified_med = models.MedicationSchedule(
            id="med_unverified",
            elderly_id="lan_1952",
            medicine_name="Panadol Extra",
            dosage="1 viên",
            instructions="Uống khi đau đầu",
            schedule_times=["14:00"],
            verified_by_caregiver=False,
        )
        db.add(unverified_med)
        db.commit()

        # Cụ kiểm tra đơn thuốc hôm nay -> Panadol KHÔNG được xuất hiện
        res_today_before = self.client.get("/api/elderly/lan_1952/medications/today")
        med_names_before = [m["medicine_name"] for m in res_today_before.json()]
        self.assertNotIn("Panadol Extra", med_names_before)

        # Người nhà gọi API xác nhận đơn thuốc (scoped by elderly_id)
        res_verify = self.client.post("/api/family/lan_1952/medications/med_unverified/verify")
        self.assertEqual(res_verify.status_code, 200)

        # Sau khi xác nhận -> Panadol xuất hiện an toàn cho Cụ
        res_today_after = self.client.get("/api/elderly/lan_1952/medications/today")
        med_names_after = [m["medicine_name"] for m in res_today_after.json()]
        self.assertIn("Panadol Extra", med_names_after)

        # 2. Người nhà tạo đơn thuốc mới qua API /api/family/medications/create -> tự động verified
        create_payload = {
            "elderly_id": "lan_1952",
            "medicine_name": "Vitamin B12",
            "pill_color_shape": "Viên nang đỏ",
            "dosage": "1 viên",
            "instructions": "Uống sau ăn trưa",
            "schedule_times": ["12:00"],
        }
        res_create = self.client.post("/api/family/medications/create", json=create_payload)
        self.assertEqual(res_create.status_code, 200)

        res_today_final = self.client.get("/api/elderly/lan_1952/medications/today")
        med_names_final = [m["medicine_name"] for m in res_today_final.json()]
        self.assertIn("Vitamin B12", med_names_final)
        db.close()

    # 12. TEST CHỐNG BRUTE-FORCE MÃ GHÉP NỐI 6 CHỮ SỐ
    def test_pairing_code_rate_limiting(self):
        # Thử sai 4 lần liên tiếp -> 404
        for _ in range(4):
            res = self.client.post("/api/elderly/pair", json={"pairing_code": "999999"})
            self.assertEqual(res.status_code, 404)

        # Lần thứ 5 sai -> 404
        res5 = self.client.post("/api/elderly/pair", json={"pairing_code": "999999"})
        self.assertEqual(res5.status_code, 404)

        # Lần thứ 6 trở đi -> Phải bị khóa HTTP 429 Too Many Requests
        res6 = self.client.post("/api/elderly/pair", json={"pairing_code": "999999"})
        self.assertEqual(res6.status_code, 429)
        self.assertIn("10 phút", res6.json()["detail"])

        # Mã đúng 123456 vẫn ghép nối thành công bình thường
        res_ok = self.client.post("/api/elderly/pair", json={"pairing_code": "123456"})
        self.assertEqual(res_ok.status_code, 200)
        self.assertEqual(res_ok.json()["full_name"], "Bà Nguyễn Thị Lan")


    # 13. TEST MẪU NHẬN DIỆN LỆNH GIỌNG NÓI FRONTEND (SOS ƯU TIÊN & TỪ CHỐI TRƯỚC ĐỒNG Ý)
    def test_frontend_voice_logic_patterns(self):
        sos_regex = re.compile(r'\b(cấp cứu|cứu tôi|cứu với|cứu con|cứu bà|cứu ông|đau ngực|tức ngực|khó thở|chóng mặt|ngất|bị ngã|ngã rồi|mệt lả|mệt quá)\b', re.IGNORECASE)

        # Ca 1: "Uống rồi mà đau ngực quá" -> Bắt buộc nhận diện là SOS khẩn cấp!
        cmd1 = "uống rồi mà đau ngực quá"
        self.assertTrue(bool(sos_regex.search(cmd1)))

        # Ca 2: "Nghiên cứu khoa học" -> Không bị bắt nhầm thành SOS
        cmd2 = "nghiên cứu khoa học"
        self.assertFalse(bool(sos_regex.search(cmd2)))

        # Ca 3: Kiểm tra logic từ chối vs xác nhận
        def evaluate_voice_intent(text: str, has_pending_bp: bool):
            lower = text.lower().strip()
            if sos_regex.search(lower) or ("cứu" in lower and "nghiên cứu" not in lower):
                return "SOS_ALERT"
            if has_pending_bp:
                is_rejection = any(w in lower for w in ["không", "sai", "nhầm", "đọc lại", "chưa đúng"])
                if is_rejection:
                    return "BP_REJECTED"
                is_confirmation = ("không" not in lower) and any(w in lower for w in [
                    "đúng rồi", "phải rồi", "chuẩn rồi", "xác nhận", "chính xác", "đúng", "phải", "ok"
                ])
                if is_confirmation:
                    return "BP_ACCEPTED"
            if any(w in lower for w in ["uống rồi", "đã uống", "xong rồi"]):
                return "MED_TAKEN"
            return "PARSE_VITALS"

        self.assertEqual(evaluate_voice_intent("không phải", True), "BP_REJECTED")
        self.assertEqual(evaluate_voice_intent("không đúng", True), "BP_REJECTED")
        self.assertEqual(evaluate_voice_intent("sai rồi con ơi", True), "BP_REJECTED")
        self.assertEqual(evaluate_voice_intent("đúng rồi con", True), "BP_ACCEPTED")
        self.assertEqual(evaluate_voice_intent("chuẩn rồi", True), "BP_ACCEPTED")
        self.assertEqual(evaluate_voice_intent("uống rồi mà đau ngực quá", True), "SOS_ALERT")
        self.assertEqual(evaluate_voice_intent("bà uống rồi", False), "MED_TAKEN")
        self.assertEqual(evaluate_voice_intent("135 trên 85", False), "PARSE_VITALS")


if __name__ == "__main__":
    unittest.main()

