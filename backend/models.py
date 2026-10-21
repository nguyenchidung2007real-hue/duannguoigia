import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy import Column, String, Integer, Float, Boolean, DateTime, ForeignKey, Text, JSON
from sqlalchemy.orm import relationship
from database import Base

VN_TIMEZONE = timezone(timedelta(hours=7))

def get_vn_now():
    """Hàm lấy thời gian hiện tại theo múi giờ chuẩn Việt Nam (UTC+7)"""
    return datetime.now(VN_TIMEZONE)

def generate_uuid():
    return str(uuid.uuid4())

class ElderlyProfile(Base):
    """Bảng hồ sơ người cao tuổi"""
    __tablename__ = "elderly_profiles"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    full_name = Column(String(100), nullable=False) # Tên gọi thân mật của cụ
    birth_year = Column(Integer, nullable=False)    # Năm sinh để AI xưng hô
    gender = Column(String(10), default="FEMALE")   # FEMALE: Bà, MALE: Ông
    address = Column(Text, nullable=True)
    medical_history = Column(Text, nullable=True)   # Bệnh nền (Huyết áp, tiểu đường...)
    drug_allergies = Column(Text, nullable=True)    # Dị ứng thuốc

    # Cơ chế Zero-Friction: Ghép nối không cần mật khẩu
    pairing_code = Column(String(6), unique=True, index=True, nullable=False)
    emergency_qr_token = Column(String(64), unique=True, index=True, nullable=False)
    presence_status = Column(String(20), default="AT_HOME") # AT_HOME / AWAY_NO_PHONE
    last_seen_at = Column(DateTime, default=get_vn_now)

    # Quan hệ
    medications = relationship("MedicationSchedule", back_populates="elderly", cascade="all, delete-orphan")
    vitals = relationship("VitalSign", back_populates="elderly", cascade="all, delete-orphan")
    family_members = relationship("FamilyMember", back_populates="elderly")
    sos_alerts = relationship("HealthAndSOSLog", back_populates="elderly")

class FamilyMember(Base):
    """Tài khoản con cái / Người chăm sóc quản lý từ xa"""
    __tablename__ = "family_members"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    elderly_id = Column(String(36), ForeignKey("elderly_profiles.id"), nullable=False)
    full_name = Column(String(100), nullable=False)
    phone_number = Column(String(20), nullable=False)
    relationship_type = Column(String(50), default="CON_TRAI") # CON_TRAI, CON_GAI, CHAU...
    is_primary_contact = Column(Boolean, default=True)
    fcm_device_token = Column(String(255), nullable=True) # Token gửi thông báo đẩy
    created_at = Column(DateTime, default=get_vn_now)

    elderly = relationship("ElderlyProfile", back_populates="family_members")

class MedicationSchedule(Base):
    """Lịch uống thuốc do con cái cài đặt từ xa hoặc quét qua OCR"""
    __tablename__ = "medication_schedules"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    elderly_id = Column(String(36), ForeignKey("elderly_profiles.id"), nullable=False)
    medicine_name = Column(String(150), nullable=False) # Tên thuốc
    pill_color_shape = Column(String(100), nullable=True) # Mô tả viên thuốc (VD: Viên tròn nhỏ màu vàng)
    dosage = Column(String(50), default="1 viên")
    instructions = Column(String(150), default="Uống sau ăn")
    schedule_times = Column(JSON, nullable=False) # Danh sách giờ uống: ["08:00", "19:30"]
    raw_ocr_text = Column(Text, nullable=True) # Đoạn text OCR quét được từ đơn thuốc
    verified_by_caregiver = Column(Boolean, default=False) # AN TOÀN Y KHOA: Mặc định False (chưa duyệt), chỉ con cái duyệt mới kích hoạt
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=get_vn_now)

    elderly = relationship("ElderlyProfile", back_populates="medications")
    logs = relationship("MedicationLog", back_populates="medication", cascade="all, delete-orphan")

class MedicationLog(Base):
    """Nhật ký uống thuốc & Điểm danh sinh tồn hàng ngày"""
    __tablename__ = "medication_logs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    medication_id = Column(String(36), ForeignKey("medication_schedules.id"), nullable=False)
    elderly_id = Column(String(36), nullable=False)
    scheduled_time = Column(DateTime, nullable=False) # Giờ dự kiến uống
    actual_taken_time = Column(DateTime, nullable=True) # Giờ thực tế cụ bấm hoặc nói đã uống
    status = Column(String(30), default="PENDING") # PENDING, REMINDED_1, REMINDED_3, TAKEN, MISSED_ESCALATED
    confirmation_method = Column(String(30), nullable=True) # BUTTON_CLICK, VOICE_CONFIRMED, FAMILY_MARKED
    reminder_count = Column(Integer, default=0) # Đếm số lần loa phát nhắc

    medication = relationship("MedicationSchedule", back_populates="logs")

class VitalSign(Base):
    """Bảng lưu chỉ số huyết áp, nhịp tim cụ đọc bằng giọng nói"""
    __tablename__ = "vital_signs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    elderly_id = Column(String(36), ForeignKey("elderly_profiles.id"), nullable=False)
    systolic = Column(Float, nullable=False)   # Huyết áp tâm thu (VD: 135)
    diastolic = Column(Float, nullable=False)  # Huyết áp tâm trương (VD: 85)
    heart_rate = Column(Float, nullable=True)  # Nhịp tim
    health_category = Column(String(30), nullable=False) # NORMAL, HIGH_NORMAL, HYPERTENSION_STAGE_1, HYPERTENSION_STAGE_2, CRITICAL_HIGH, HYPOTENSION_LOW, HYPOTENSION_MILD, CRITICAL_LOW, MIXED_DANGER
    input_source = Column(String(30), default="VOICE_INPUT")
    raw_voice_transcript = Column(Text, nullable=True) # Câu gốc: "Huyết áp hôm nay 135 trên 85"
    measured_at = Column(DateTime, default=get_vn_now)

    elderly = relationship("ElderlyProfile", back_populates="vitals")

class HealthAndSOSLog(Base):
    """Lưu trữ lịch sử cảnh báo SOS & Tình huống khẩn cấp (sẵn sàng khi kích hoạt)"""
    __tablename__ = "health_and_sos_logs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    elderly_id = Column(String(36), ForeignKey("elderly_profiles.id"), nullable=False)
    trigger_source = Column(String(30), nullable=False) # RED_BUTTON, PHOBERT_VOICE, VITAL_CRITICAL, DEADMAN_MISSED, PUBLIC_QR_SCAN
    escalation_status = Column(String(30), default="TRIGGERED") # TRIGGERED, CHECKING_15S, BROADCASTED_FAMILY, RESOLVED
    raw_transcript = Column(Text, nullable=True)
    audio_clip_url = Column(String(255), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    location_note = Column(String(150), default="Tại nhà")
    triggered_at = Column(DateTime, default=get_vn_now)
    resolved_at = Column(DateTime, nullable=True)

    elderly = relationship("ElderlyProfile", back_populates="sos_alerts")
