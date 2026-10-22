from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

# Schema cho Hồ sơ Người cao tuổi
class ElderlyProfileCreate(BaseModel):
    full_name: str
    birth_year: int
    gender: str = "FEMALE"
    address: Optional[str] = None
    medical_history: Optional[str] = None
    drug_allergies: Optional[str] = None

class ElderlyProfileResponse(ElderlyProfileCreate):
    id: str
    pairing_code: str
    emergency_qr_token: str
    presence_status: str
    last_seen_at: datetime

    class Config:
        from_attributes = True

# Schema ghép nối 6 số không mật khẩu
class PairRequest(BaseModel):
    pairing_code: str

# Schema Lịch uống thuốc
class MedicationCreate(BaseModel):
    elderly_id: str
    medicine_name: str
    pill_color_shape: Optional[str] = "Viên thuốc tròn"
    dosage: str = "1 viên"
    instructions: str = "Uống sau ăn"
    schedule_times: List[str] = ["08:00"]

class MedicationResponse(MedicationCreate):
    id: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True

# Schema xác nhận hàng loạt thuốc từ OCR đơn thuốc
class PrescriptionBatchCreate(BaseModel):
    medications: List[MedicationCreate]
    raw_ocr_text: Optional[str] = None

# Schema Xác nhận đã uống thuốc
class MedicationTakeRequest(BaseModel):
    confirmation_method: str = "BUTTON_CLICK"

# Schema Phân tích Giọng nói (Bước 1: Parse & Validate, chưa lưu)
class VitalSignParseInput(BaseModel):
    elderly_id: str
    raw_transcript: str

# Schema Lưu chính thức sau khi Cụ xác nhận (Bước 2: Confirm & Commit)
class VitalSignConfirmInput(BaseModel):
    elderly_id: str
    systolic: float
    diastolic: float
    heart_rate: Optional[float] = None
    raw_transcript: Optional[str] = None

class VitalSignResponse(BaseModel):
    id: str
    elderly_id: str
    systolic: float
    diastolic: float
    heart_rate: Optional[float]
    health_category: str
    raw_voice_transcript: Optional[str] = None
    measured_at: datetime

    class Config:
        from_attributes = True

# Schema gửi tín hiệu cứu hộ từ mã QR công cộng
class QrAlertRequest(BaseModel):
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    helper_note: Optional[str] = None

# Schema Quản lý Danh bạ Khẩn cấp Người thân
class EmergencyContactCreate(BaseModel):
    full_name: str
    phone_number: str
    relationship_type: str = "CON_GAI"
    is_primary_contact: bool = False

class EmergencyContactResponse(EmergencyContactCreate):
    id: str
    elderly_id: str
    created_at: datetime

    class Config:
        from_attributes = True

# ================================================================
# Schema ConversationLog — MLOps data collection cho fine-tuning
# Mỗi turn hội thoại được lưu → export .jsonl cho Qwen2.5-3B & PhoBERT
# ================================================================
class ConversationLogCreate(BaseModel):
    elderly_id: str
    user_input: str
    input_source: str = "VOICE"          # VOICE | BUTTON | SYSTEM
    detected_intent: Optional[str] = None # SOS | VITAL_LOG | MED_CONFIRM | CHITCHAT | UNKNOWN
    intent_confidence: Optional[float] = None
    ai_response: Optional[str] = None
    response_source: str = "RULE_BASED"  # RULE_BASED | QWEN | PHOBERT
    session_id: Optional[str] = None

class ConversationFeedback(BaseModel):
    caregiver_feedback: str              # CORRECT | WRONG | IGNORED
    corrected_response: Optional[str] = None

class ConversationLogResponse(ConversationLogCreate):
    id: str
    caregiver_feedback: Optional[str] = None
    corrected_response: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True
