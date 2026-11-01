"""家长信群发链接 — 公开路由（无需登录）。

群发不让老师本地下发 docx，而是给每个学生生成一个不可猜 token 链接。
有家长手机号时用"手机后4位"二次校验；没有手机号时 token 本身作为访问凭证。

安全要点：
- token 由 secrets.token_urlsafe(32) 生成（~256bit，不可枚举）。
- meta 阶段不返回任何 PII；是否需要校验只反映链接的保护方式。
- 手机后4位仅 1 万组合 → fail_count 超 MAX_FAIL_ATTEMPTS 即锁死该 token 防枚举。
- 撤销 / 过期 / 锁死 三态对外统一表现为 404，不区分原因。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import ParentLetterShare
from models_user import User
from routers.auth import _require_user

logger = logging.getLogger(__name__)
router = APIRouter()

# 统一的"不可访问"文案：无效 token / 过期 / 撤销 / 锁死 全部走这个，杜绝侧信道
_NOT_FOUND = "链接无效或已失效"


class VerifyRequest(BaseModel):
    last4: str = Field(..., min_length=4, max_length=4, pattern=r"^\d{4}$")


def _live_share(db: Session, token: str) -> ParentLetterShare:
    """取一条仍然有效的分享记录；任何失效原因都统一抛 404。"""
    rec = db.query(ParentLetterShare).filter(ParentLetterShare.token == token).first()
    if not rec or not rec.is_valid:
        raise HTTPException(404, detail=_NOT_FOUND)
    return rec


@router.get("/letter/{token}/meta")
def letter_meta(token: str, db: Session = Depends(get_db)):
    """家长打开链接时的首屏元信息，不泄露任何 PII。"""
    rec = _live_share(db, token)
    if rec.phone_last4:
        return {"need_verify": True, "hint": "请输入家长手机号后4位查看"}
    return {"need_verify": False, "hint": "该链接无需密码即可查看"}


@router.post("/letter/{token}/verify")
def letter_verify(token: str, req: VerifyRequest, db: Session = Depends(get_db)):
    """校验手机后4位。失败累计，超阈值锁死 token 防暴力枚举。

    成功：返回信件 HTML，view_count+1，重置 fail_count。
    失败：fail_count+1；达到上限后该 token 永久锁死（统一 404）。
    """
    rec = _live_share(db, token)

    if req.last4 != (rec.phone_last4 or ""):
        rec.fail_count = (rec.fail_count or 0) + 1
        db.commit()
        # 锁死后与其它失效原因一样：统一 404，不提示"已锁定"以免泄露状态
        if rec.is_locked:
            raise HTTPException(404, detail=_NOT_FOUND)
        remaining = rec.MAX_FAIL_ATTEMPTS - rec.fail_count
        raise HTTPException(403, detail=f"校验失败，还可尝试 {max(remaining, 0)} 次")

    rec.fail_count = 0
    rec.view_count = (rec.view_count or 0) + 1
    db.commit()
    return {"ok": True, "html": rec.html}


@router.get("/letter/{token}/view", response_class=HTMLResponse)
def letter_view_page(token: str, db: Session = Depends(get_db)):
    """家长访问的落地页（/api 前缀版）。"""
    rec = _live_share(db, token)
    if not rec.phone_last4:
        rec.view_count = (rec.view_count or 0) + 1
        db.commit()
        return HTMLResponse(content=rec.html)
    return HTMLResponse(content=build_gate_page(token))


def build_gate_page(
    token: str,
    title: str = "致家长的一封信",
    description_html: str = "为保护学生隐私，请输入<b>家长手机号后4位</b>查看",
    button_text: str = "查看信件",
    verify_url: str | None = None,
) -> str:
    """极简校验框落地页：不含任何 PII，token 由服务端注入（不靠路径解析）。

    校验通过后用 document.write 整页替换为信件 HTML。
    """
    from html import escape
    safe_token = escape(token, quote=True)
    safe_title = escape(title)
    safe_description = description_html
    safe_button_text = escape(button_text)
    safe_verify_url = escape(verify_url or f"/api/letter/{token}/verify", quote=True)
    return """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  body{margin:0;background:#FFFDF7;font-family:"SimSun","Songti SC",serif;color:#333;}
  .gate{max-width:420px;margin:64px auto;background:#FFFDF7;border:1px solid #C5A55A;padding:34px 28px;
        box-shadow:0 2px 12px rgba(0,0,0,.06);text-align:center;}
  .gate:before,.gate:after{content:"";display:block;border-top:3px double #C5A55A;margin:0 0 22px;}
  .gate:after{margin:22px 0 0;}
  .gate h2{color:#1A237E;font-family:"SimHei",sans-serif;font-size:24px;margin:0 0 12px;}
  .gate p{background:#F5F5F5;border:1px solid #E0E0E0;color:#333;font-size:13px;margin:0 0 22px;padding:12px;}
  .gate input{width:100%;font-size:18px;letter-spacing:6px;text-align:center;padding:12px;
        border:1px solid #E0E0E0;border-radius:4px;box-sizing:border-box;background:#fff;}
  .gate button{width:100%;margin-top:16px;background:#1A237E;color:#fff;border:0;border-radius:4px;
        padding:12px;font-size:16px;cursor:pointer;}
  .gate button:disabled{opacity:.6;cursor:not-allowed;}
  .err{color:#c0392b;font-size:13px;min-height:18px;margin-top:10px;}
</style></head>
<body>
  <div class="gate" id="gate">
    <h2>__TITLE__</h2>
    <p>__DESCRIPTION__</p>
    <input id="last4" inputmode="numeric" maxlength="4" placeholder="****" autocomplete="off">
    <button id="btn">__BUTTON_TEXT__</button>
    <div class="err" id="err"></div>
  </div>
  <script>
    var TOKEN = "__TOKEN__";
    var btn = document.getElementById('btn');
    var inp = document.getElementById('last4');
    var err = document.getElementById('err');
    function submit(){
      var v = (inp.value||'').trim();
      if(!/^\\d{4}$/.test(v)){ err.textContent='请输入4位数字'; return; }
      btn.disabled = true; err.textContent='';
      fetch('__VERIFY_URL__',{
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({last4:v})
      }).then(function(r){ return r.json().then(function(d){ return {ok:r.ok,d:d}; }); })
        .then(function(res){
          if(res.ok && res.d.html){
            document.open(); document.write(res.d.html); document.close();
          } else {
            err.textContent = (res.d && res.d.detail) || '校验失败';
            btn.disabled = false;
          }
        }).catch(function(){ err.textContent='网络错误，请重试'; btn.disabled=false; });
    }
    btn.addEventListener('click', submit);
    inp.addEventListener('keydown', function(e){ if(e.key==='Enter') submit(); });
  </script>
</body></html>""".replace("__TOKEN__", safe_token) \
        .replace("__TITLE__", safe_title) \
        .replace("__DESCRIPTION__", safe_description) \
        .replace("__BUTTON_TEXT__", safe_button_text) \
        .replace("__VERIFY_URL__", safe_verify_url)


# ── 班级统一链接：已停用 ──

_CLASS_LINK_DISABLED = "班级统一链接已停用，请联系老师获取学生专属链接"


@router.post("/class/{class_id}/parent-link")
def create_class_parent_link(
    class_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """Fail closed: four phone digits cannot authorize access to a class."""
    raise HTTPException(410, detail=_CLASS_LINK_DISABLED)


@router.get("/letter/class/{token}")
def class_letter_page(token: str):
    raise HTTPException(410, detail=_CLASS_LINK_DISABLED)


@router.post("/letter/class/{token}/verify")
def class_letter_verify(token: str, req: VerifyRequest, db: Session = Depends(get_db)):
    raise HTTPException(410, detail=_CLASS_LINK_DISABLED)
