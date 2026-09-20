"""Аутентификация, профиль и сброс пароля."""
import logging
import secrets
from datetime import datetime, timedelta
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi_mail import MessageSchema
from sqlalchemy.orm import Session
from app.config import FRONTEND_URL
from app.db import get_db
from app.deps import get_current_user
from app.mailer import send_email
from app.models import PasswordResetToken, User
from app.schemas import (
                        LoginRequest, PasswordReset, PasswordResetRequest,
                        PasswordResetResponse, RegisterRequest, Token, UserResponse
)
from app.security import create_access_token, get_password_hash, verify_password

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/api/register", response_model=UserResponse)
async def register(request: RegisterRequest, db: Session = Depends(get_db)):
    existing_user = db.query(User).filter(User.email == request.email).first()
    if existing_user:
        raise HTTPException(400, detail="Email уже зарегистрирован")
    
    hashed_password = get_password_hash(request.password)
    db_user = User(
        name=request.name,
        email=request.email,
        hashed_password=hashed_password
    )
    db.add(db_user)
    db.commit()
    return db_user

@router.post("/api/login", response_model=Token)
async def login(request: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == request.email).first()
    if not user or not verify_password(request.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Неверные учетные данные")
    
    access_token = create_access_token(data={"sub": user.email})
    return {"access_token": access_token, "token_type": "bearer"}

@router.get("/api/me", response_model=UserResponse)
async def get_current_user_endpoint(current_user: User = Depends(get_current_user)):
    return current_user

# Эндпоинт для обновления профиля
@router.put("/api/profile")
def update_profile(
    profile_data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.id == current_user.id).first()
    if not user:
        raise HTTPException(404, detail="User not found")
    
    # Обновляем данные профиля
    if "name" in profile_data:
        user.name = profile_data["name"]
    if "email" in profile_data:
        user.email = profile_data["email"]
    if "position" in profile_data:
        user.position = profile_data["position"]
    if "company" in profile_data:
        user.company = profile_data["company"]
    if "website" in profile_data:
        user.website = profile_data["website"]
    if "about" in profile_data:
        user.about = profile_data["about"]
    if "password" in profile_data and profile_data["password"]:
        user.hashed_password = get_password_hash(profile_data["password"])
    
    db.commit()
    
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "position": user.position,
        "company": user.company,
        "website": user.website,
        "about": user.about
    }

# Эндпоинт для запроса сброса пароля
@router.post("/api/reset-password-request", response_model=PasswordResetResponse)
async def request_password_reset(
    request: PasswordResetRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    try:
        # Проверяем, существует ли пользователь с таким email
        user = db.query(User).filter(User.email == request.email.lower()).first()
        
        if not user:
            # Возвращаем успех даже если пользователь не найден (безопасность)
            return PasswordResetResponse(
                success=True,
                message="Если ваш email зарегистрирован в системе, вы получите инструкции по восстановлению пароля"
            )
        
        # Удаляем предыдущие неиспользованные токены для этого email
        db.query(PasswordResetToken).filter(
            PasswordResetToken.email == request.email.lower(),
            PasswordResetToken.used == False
        ).delete()
        
        # Генерируем новый токен
        token = secrets.token_urlsafe(32)
        reset_token = PasswordResetToken(
            email=request.email.lower(),
            token=token,
            expires_at=datetime.utcnow() + timedelta(hours=1)
        )
        db.add(reset_token)
        db.commit()
        
        # Формируем ссылку для сброса пароля
        reset_link = f"{FRONTEND_URL}/reset-password?token={token}"
        
        # Создаем email сообщение
        message = MessageSchema(
            subject="Восстановление пароля - ВкусВилл BPMN",
            recipients=[request.email],
            html=f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                <div style="background: linear-gradient(135deg, #00A550 0%, #F62369 100%); padding: 30px; text-align: center; border-radius: 10px 10px 0 0;">
                    <h1 style="color: white; margin: 0; font-size: 28px;">ВкусВилл BPMN</h1>
                </div>
                <div style="background: white; padding: 40px; border-radius: 0 0 10px 10px; box-shadow: 0 5px 20px rgba(0,0,0,0.1);">
                    <h2 style="color: #333; margin-top: 0;">Восстановление пароля</h2>
                    <p style="color: #666; line-height: 1.6;">
                        Здравствуйте!<br><br>
                        Вы получили это письмо, потому что был запрошен сброс пароля для вашей учетной записи в ВкусВилл BPMN.<br><br>
                        Для сброса пароля нажмите на кнопку ниже:
                    </p>
                    <div style="text-align: center; margin: 30px 0;">
                        <a href="{reset_link}" style="
                            background: linear-gradient(135deg, #00A550 0%, #F62369 100%);
                            color: white;
                            padding: 15px 30px;
                            text-decoration: none;
                            border-radius: 50px;
                            font-weight: 600;
                            display: inline-block;
                        ">Сбросить пароль</a>
                    </div>
                    <p style="color: #666; line-height: 1.6;">
                        Или скопируйте и вставьте следующую ссылку в браузер:<br>
                        <a href="{reset_link}" style="color: #00A550;">{reset_link}</a>
                    </p>
                    <p style="color: #666; line-height: 1.6;">
                        Эта ссылка действительна в течение 1 часа.<br><br>
                        Если вы не запрашивали сброс пароля, просто проигнорируйте это письмо.<br><br>
                        С уважением,<br>
                        Команда ВкусВилл BPMN
                    </p>
                </div>
            </div>
            """,
            subtype="html"
        )
        
        # Отправляем email в фоне
        background_tasks.add_task(send_email, message)
        
        return PasswordResetResponse(
            success=True,
            message="Если ваш email зарегистрирован в системе, вы получите инструкции по восстановлению пароля"
        )
        
    except Exception as e:
        # Логируем ошибку
        logger.error("Error in password reset request: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Произошла ошибка при отправке запроса на восстановление пароля"
        )

# Эндпоинт для сброса пароля
@router.post("/api/reset-password", response_model=PasswordResetResponse)
async def reset_password(
    request: PasswordReset,
    db: Session = Depends(get_db)
):
    try:
        # Ищем токен в базе данных
        reset_token = db.query(PasswordResetToken).filter(
            PasswordResetToken.token == request.token,
            PasswordResetToken.used == False,
            PasswordResetToken.expires_at > datetime.utcnow()
        ).first()
        
        if not reset_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Недействительный или просроченный токен сброса пароля"
            )
        
        # Находим пользователя по email из токена
        user = db.query(User).filter(User.email == reset_token.email).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Пользователь не найден"
            )
        
        # Валидация нового пароля
        if len(request.new_password) < 8:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Пароль должен содержать минимум 8 символов"
            )
        
        # Обновляем пароль пользователя
        user.hashed_password = get_password_hash(request.new_password)
        
        # Помечаем токен как использованный
        reset_token.used = True
        
        db.commit()
        
        return PasswordResetResponse(
            success=True,
            message="Пароль успешно изменен"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        # Логируем ошибку
        logger.error("Error in password reset: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Произошла ошибка при сбросе пароля"
        )

# Эндпоинт для проверки токена
@router.post("/api/verify-reset-token")
async def verify_reset_token(
    token: str,
    db: Session = Depends(get_db)
):
    try:
        reset_token = db.query(PasswordResetToken).filter(
            PasswordResetToken.token == token,
            PasswordResetToken.used == False,
            PasswordResetToken.expires_at > datetime.utcnow()
        ).first()
        
        if not reset_token:
            return {"valid": False}
        
        return {"valid": True}
        
    except Exception as e:
        logger.error("Error verifying reset token: %s", e, exc_info=True)
        return {"valid": False}
