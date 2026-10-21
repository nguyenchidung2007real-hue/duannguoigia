"""
Dịch vụ kiểm tra và phân loại chỉ số huyết áp theo chuẩn Y tế (Rule-based Guardrail).
Đảm bảo AI không bao giờ phán đoán sai lệch về mặt y khoa.
"""

def evaluate_blood_pressure(systolic: float, diastolic: float, pulse: float = None) -> dict:
    """
    Phân loại huyết áp theo tiêu chuẩn Hội Tim mạch học Việt Nam (VNHA / VSH) & WHO.
    
    systolic: Huyết áp tâm thu (số trên, mmHg)
    diastolic: Huyết áp tâm trương (số dưới, mmHg)
    pulse: Nhịp tim (lần/phút, tùy chọn)
    """
    # 1. Kiểm tra tính hợp lệ cơ bản
    if systolic <= diastolic:
        return {
            "category": "INVALID",
            "level": 0,
            "title": "Chỉ số không hợp lệ",
            "advice": "Huyết áp tâm thu (số trên) phải lớn hơn huyết áp tâm trương (số dưới). Cụ đo lại giúp con nhé ạ!"
        }

    # 2. Ngưỡng báo động cấp cứu
    if systolic >= 180 or diastolic >= 110:
        return {
            "category": "CRITICAL_HIGH",
            "level": 4,
            "title": "Huyết áp rất cao (Cần chú ý khẩn cấp)",
            "advice": "Dạ thưa cụ, chỉ số huyết áp của cụ đang khá cao ạ. Cụ hãy ngồi tựa lưng nghỉ ngơi ngay, đừng đi lại và uống một ngụm nước ấm nhé. Con đang gửi thông báo cho gia đình mình đây ạ!"
        }

    # 3. Tăng huyết áp độ 2
    if (160 <= systolic < 180) or (100 <= diastolic < 110):
        return {
            "category": "HYPERTENSION_STAGE_2",
            "level": 3,
            "title": "Tăng huyết áp độ 2",
            "advice": "Dạ thưa cụ, huyết áp hôm nay của cụ hơi cao hơn bình thường một chút. Cụ nhớ kiểm tra xem hôm nay mình đã uống thuốc huyết áp theo cữ chưa nhé ạ. Cụ ngồi nghỉ ngơi thả lỏng người giúp con ạ!"
        }

    # 4. Tăng huyết áp độ 1
    if (140 <= systolic < 160) or (90 <= diastolic < 100):
        return {
            "category": "HYPERTENSION_STAGE_1",
            "level": 2,
            "title": "Tăng huyết áp độ 1",
            "advice": "Dạ thưa cụ, huyết áp của cụ hơi cao nhẹ một chút so với mức chuẩn. Cụ nhớ uống đủ nước và hạn chế ăn mặn nhé ạ!"
        }

    # 5. Tiền tăng huyết áp (Hơi cao nhẹ)
    if (120 <= systolic < 140) or (80 <= diastolic < 90):
        return {
            "category": "HIGH_NORMAL",
            "level": 1,
            "title": "Huyết áp bình thường cao",
            "advice": "Dạ thưa cụ, chỉ số huyết áp của cụ ở mức khá tốt và an toàn ạ. Cụ duy trì tinh thần vui vẻ, sinh hoạt điều độ như thế này nhé!"
        }

    # 6. Huyết áp bình thường / Tối ưu
    if (90 <= systolic < 120) and (60 <= diastolic < 80):
        return {
            "category": "NORMAL",
            "level": 0,
            "title": "Huyết áp rất đẹp (Chuẩn)",
            "advice": "Dạ thưa cụ, huyết áp của cụ hôm nay rất đẹp và ổn định ạ! Cụ tiếp tục duy trì sức khỏe thật tốt nhé ạ!"
        }

    # 7. Huyết áp thấp
    if systolic < 90 or diastolic < 60:
        return {
            "category": "LOW_BP",
            "level": 2,
            "title": "Huyết áp thấp",
            "advice": "Dạ thưa cụ, huyết áp hôm nay hơi thấp một chút. Cụ ngồi nghỉ ngơi, uống một cốc nước ấm hoặc trà gừng ấm nhé ạ!"
        }

    return {
        "category": "NORMAL",
        "level": 0,
        "title": "Huyết áp bình thường",
        "advice": "Dạ thưa cụ, huyết áp của cụ hoàn toàn ổn định ạ!"
    }
