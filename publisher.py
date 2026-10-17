"""ناشر صلة — ينشر منشورات queue.json على صفحة فيسبوك وحساب إنستغرام عبر Meta Graph API.
الأنواع: post (صورة أو كاروسيل)، story (صورة)، reel (فيديو mp4 في الحقل video).

الأوامر:
  python publisher.py check    فحص الطابور والصور والفيديو بلا اتصال بـ Meta
  python publisher.py whoami   فحص المفتاح: اسم الصفحة وحساب إنستغرام وحصة النشر
  python publisher.py run      نشر أو جدولة ما حان وقته، مرة واحدة
  python publisher.py videotest  اختبار رفع ريلز media/test/reel-test.mp4 بلا نشر
  python publisher.py loop     «مناوبة»: يكرر run كل دقيقتين قرابة 5 ساعات ونصف، ويحفظ الحالة
                               في المستودع بعد كل تغيير. يشغّله GitHub Actions، وكل مناوبة تبدأ التالية.

متغيرات البيئة:
  META_TOKEN      مفتاح مستخدم النظام (سرّي، يُحفظ في GitHub Secrets فقط)
  PAGE_ID         معرّف صفحة فيسبوك
  MEDIA_BASE_URL  الرابط العام لجذر المستودع، تُبنى منه روابط الصور
  GRAPH_VERSION   اختياري، الافتراضي v26.0
  GITHUB_TOKEN, GITHUB_REPOSITORY  من GitHub Actions: لفتح Issue تنبيه عند الفشل
  LOOP_MINUTES    اختياري، مدة المناوبة، الافتراضي 330

لا يعتمد إلا على مكتبة بايثون القياسية، حتى يعمل على GitHub Actions بلا تثبيت.
"""
import json
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
QUEUE_PATH = os.path.join(ROOT, "queue.json")
GRAPH = "https://graph.facebook.com/" + os.environ.get("GRAPH_VERSION", "v26.0")

# Meta تقبل جدولة منشور الصفحة بين 10 دقائق و30 يوماً مقدّماً؛ نترك هامشاً من الطرفين.
FB_SCHEDULE_MIN_LEAD = timedelta(minutes=20)
FB_SCHEDULE_MAX_LEAD = timedelta(days=29)
# منشور فات موعده بأكثر من هذا لا يُنشر متأخراً، بل يُعلَّم فاشلاً لينتبه المالك.
# المناوبات المتصلة تجعل التأخير دقائق؛ الهامش الواسع لانقطاع نادر في GitHub فقط.
LATE_LIMIT = timedelta(hours=12)
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=15)  # مهلة بين محاولات العنصر الفاشل
LOOP_SLEEP = 120  # ثوانٍ بين دورات المناوبة


class GraphError(Exception):
    pass


class PermanentError(Exception):
    """خطأ لن تحلّه إعادة المحاولة: يُعلَّم العنصر فاشلاً نهائياً من أول مرة."""


def api(method, path, token, **params):
    params["access_token"] = token
    body = urllib.parse.urlencode(params)
    if method == "POST":
        req = urllib.request.Request(f"{GRAPH}/{path}", data=body.encode(), method="POST")
    else:  # GET أو DELETE: المعاملات في الرابط
        req = urllib.request.Request(f"{GRAPH}/{path}?{body}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        # رسالة خطأ Meta لا تحتوي المفتاح؛ لا نطبع الرابط لأنه يحتويه.
        detail = e.read().decode(errors="replace")[:600]
        raise GraphError(f"{method} {path} → HTTP {e.code}: {detail}") from None


# ---------- الطابور ----------

def load_queue():
    with open(QUEUE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_queue(queue):
    tmp = QUEUE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, QUEUE_PATH)


def when(item):
    t = datetime.fromisoformat(item["publish_at"])
    if t.tzinfo is None:
        raise ValueError(f"{item['id']}: publish_at بلا منطقة زمنية")
    return t.astimezone(timezone.utc)


def media_url(path):
    base = os.environ["MEDIA_BASE_URL"].rstrip("/")
    return base + "/" + urllib.parse.quote(path)


def rupload(url, token, path):
    """يرفع ملف الفيديو نفسه إلى خادم رفع Meta. لا نعطي Meta رابطاً: GitHub يقدّم ملفات mp4
    بنوع application/octet-stream، فالرفع المباشر أضمن."""
    with open(os.path.join(ROOT, path), "rb") as f:
        data = f.read()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(data))})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:600]
        raise GraphError(f"رفع {path} → HTTP {e.code}: {detail}") from None


def mp4_info(path):
    """(العرض، الارتفاع، المدة بالثواني) من صندوقَي mvhd وtkhd، بلا مكتبات خارجية. None إن تعذّر."""
    with open(path, "rb") as f:
        data = f.read()
    i = data.find(b"mvhd")
    if i < 0:
        return None
    v = i + 4
    if data[v] == 1:
        scale, dur = struct.unpack(">IQ", data[v + 20:v + 32])
    else:
        scale, dur = struct.unpack(">II", data[v + 12:v + 20])
    w = h = 0
    j = data.find(b"tkhd")
    while j >= 0 and not w:
        v = j + 4
        off = 88 if data[v] == 1 else 76
        w, h = (x >> 16 for x in struct.unpack(">II", data[v + off:v + off + 8]))
        j = data.find(b"tkhd", j + 4)
    return (w, h, dur / scale) if scale and w else None


# ---------- فيسبوك ----------

def fb_upload_photo(page_id, token, path, **extra):
    return api("POST", f"{page_id}/photos", token, url=media_url(path), published="false", **extra)["id"]


def fb_post(page_id, token, item, schedule_ts=None):
    """منشور صورة واحدة يُجدول على خوادم Meta. منشور متعدد الصور يُنشر في موعده فقط (schedule_ts=None)."""
    images = item["images"]
    if len(images) == 1:
        params = {"url": media_url(images[0]), "message": item["caption"]}
        if schedule_ts:
            params.update(published="false", scheduled_publish_time=str(schedule_ts))
        return api("POST", f"{page_id}/photos", token, **params)
    if schedule_ts:
        raise ValueError("منشور فيسبوك متعدد الصور لا يُجدول مسبقاً")
    # الصور تُرفع غير منشورة ثم تُرفق بمنشور واحد يُنشر فوراً.
    # لا نستعمل temporary=true (اللازمة للجدولة): Meta رفضتها لهذه الصفحة بـ
    # «(#200) You do not have permission to create an unpublished post» (3 و4 تشرين الأول)،
    # بينما الرفع غير المنشور العادي ينجح (ستوري 4 تشرين الأول).
    try:
        ids = [fb_upload_photo(page_id, token, p) for p in images]
        return api("POST", f"{page_id}/feed", token, message=item["caption"],
                   attached_media=json.dumps([{"media_fbid": i} for i in ids]))
    except GraphError as e:
        # رفض من Meta (4xx): الشريحة الأولى منشوراً واحداً مع النص كاملاً، أفضل من لا شيء.
        # عطل مؤقت (5xx أو شبكة): يُعاد المنشور كاملاً في محاولة لاحقة.
        if "→ HTTP 4" not in str(e):
            raise
        res = fb_post(page_id, token, dict(item, images=images[:1]))
        res["fallback"] = "single-image: " + str(e)[:300]
        return res


def fb_reel(page_id, token, item, state="PUBLISHED"):
    """ريلز فيسبوك: بدء الرفع، رفع الملف، ثم الإنهاء بالحالة المطلوبة (PUBLISHED أو DRAFT للاختبار)."""
    start = api("POST", f"{page_id}/video_reels", token, upload_phase="start")
    rupload(start["upload_url"], token, item["video"])
    res = api("POST", f"{page_id}/video_reels", token, upload_phase="finish",
              video_id=start["video_id"], video_state=state, description=item.get("caption", ""))
    res["video_id"] = start["video_id"]
    return res


def fb_story(page_id, token, item):
    try:
        photo_id = fb_upload_photo(page_id, token, item["images"][0])
    except GraphError as e:
        if "(#200)" in str(e) and "unpublished" in str(e):
            # لا جدوى من إعادة المحاولة: القيد من وضع التطبيق لا من الشبكة.
            raise PermanentError("ستوري فيسبوك يحتاج تطبيقاً في الوضع الحيّ (Live): " + str(e)[:200]) from None
        raise
    return api("POST", f"{page_id}/photo_stories", token, photo_id=photo_id)


# ---------- إنستغرام ----------

def ig_wait(container_id, token, tries=30, every=5):
    for _ in range(tries):
        info = api("GET", container_id, token, fields="status_code,status")
        status = info.get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise GraphError(f"حاوية إنستغرام {container_id} حالتها {status}: {info.get('status', '')}")
        time.sleep(every)
    raise GraphError(f"حاوية إنستغرام {container_id} لم تجهز خلال {tries * every // 60} دقيقة")


def ig_reel_container(ig_id, token, item):
    """حاوية ريلز إنستغرام برفع مباشر للملف، تنتظر حتى تنتهي معالجة الفيديو (حتى 10 دقائق)."""
    res = api("POST", f"{ig_id}/media", token, media_type="REELS", upload_type="resumable",
              caption=item.get("caption", ""), share_to_feed="true")
    rupload(res["uri"], token, item["video"])
    ig_wait(res["id"], token, tries=60, every=10)
    return res["id"]


def ig_publish(ig_id, token, item):
    if item["kind"] == "reel":
        return api("POST", f"{ig_id}/media_publish", token, creation_id=ig_reel_container(ig_id, token, item))
    images = item["images"]
    if item["kind"] == "story":
        cid = api("POST", f"{ig_id}/media", token, media_type="STORIES", image_url=media_url(images[0]))["id"]
    elif len(images) == 1:
        cid = api("POST", f"{ig_id}/media", token, image_url=media_url(images[0]), caption=item["caption"])["id"]
    else:
        children = []
        for p in images:
            child = api("POST", f"{ig_id}/media", token, image_url=media_url(p), is_carousel_item="true")["id"]
            ig_wait(child, token)
            children.append(child)
        cid = api("POST", f"{ig_id}/media", token, media_type="CAROUSEL",
                  children=",".join(children), caption=item["caption"])["id"]
    ig_wait(cid, token)
    return api("POST", f"{ig_id}/media_publish", token, creation_id=cid)


# ---------- الأوامر ----------

def connect():
    token, page_id = os.environ["META_TOKEN"], os.environ["PAGE_ID"]
    page = api("GET", page_id, token, fields="name,access_token,instagram_business_account{id,username}")
    ig = page.get("instagram_business_account") or {}
    return page, page["access_token"], ig.get("id"), token


def cmd_whoami():
    page, _, ig_id, token = connect()
    ig = page.get("instagram_business_account") or {}
    print(f"الصفحة: {page['name']} ({page['id']})")
    print(f"إنستغرام: @{ig.get('username', '—')} ({ig_id or 'غير مربوط'})")
    if ig_id:
        quota = api("GET", f"{ig_id}/content_publishing_limit", token, fields="quota_usage,config")
        print("حصة نشر إنستغرام:", json.dumps(quota.get("data", quota), ensure_ascii=False))


def jpeg_size(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


def check_reel(item):
    """حدود Meta للريلز: فيسبوك 3–90 ثانية بنسبة 9:16 وعرض 540 على الأقل؛ إنستغرام 3 ثوانٍ إلى 15 دقيقة."""
    p = item.get("video")
    if not p:
        return ["الريلز بلا ملف فيديو (video)"]
    full = os.path.join(ROOT, p)
    if not os.path.exists(full):
        return [f"الفيديو غير موجود: {p}"]
    if not p.lower().endswith(".mp4"):
        return [f"الفيديو ليس mp4: {p}"]
    issues = []
    if os.path.getsize(full) > 95 * 1024 * 1024:
        issues.append(f"الفيديو أكبر من 95MB (حد GitHub 100MB): {p}")
    info = mp4_info(full)
    if not info:
        return issues + [f"تعذّرت قراءة أبعاد الفيديو ومدته: {p}"]
    w, h, secs = info
    if not 0.55 <= w / h <= 0.58 or w < 540:
        issues.append(f"أبعاد الفيديو {w}×{h}؛ الريلز يحتاج 9:16 بعرض 540 على الأقل: {p}")
    if not 3 <= secs <= 90:
        issues.append(f"مدة الفيديو {secs:.1f} ثانية؛ ريلز فيسبوك من 3 إلى 90 ثانية: {p}")
    return issues


def cmd_check():
    queue = load_queue()
    problems = 0
    for item in queue["items"]:
        issues = []
        try:
            t = when(item)
        except Exception as e:
            issues.append(str(e))
            t = None
        imgs = item.get("images", [])
        if item["kind"] == "story" and len(imgs) != 1:
            issues.append("الستوري يحتاج صورة واحدة")
        if item["network"] == "instagram" and item["kind"] == "post" and len(imgs) > 10:
            issues.append("كاروسيل إنستغرام 10 صور كحد أقصى")
        for p in imgs:
            full = os.path.join(ROOT, p)
            if not os.path.exists(full):
                issues.append(f"الصورة غير موجودة: {p}")
                continue
            size = jpeg_size(full)
            if not size:
                issues.append(f"ليست JPEG: {p}")
            elif item["network"] == "instagram" and item["kind"] == "post":
                ratio = size[0] / size[1]
                if not 0.8 <= ratio <= 1.91:
                    issues.append(f"نسبة أبعاد غير مقبولة في إنستغرام {size}: {p}")
            if os.path.getsize(full) > 8 * 1024 * 1024:
                issues.append(f"الصورة أكبر من 8MB: {p}")
        if item["kind"] == "reel":
            issues += check_reel(item)
        cap = item.get("caption", "")
        if item["kind"] in ("post", "reel") and not cap:
            issues.append("المنشور بلا نص")
        if len(cap) > 2200 or cap.count("#") > 30:
            issues.append("النص أطول من حدود إنستغرام")
        mark = "✓" if not issues else "✗"
        local = t.astimezone(timezone(timedelta(hours=3))).strftime("%a %d/%m %H:%M") if t else "?"
        print(f"{mark} {item['id']:<34} {local}  {item['status']:<10} approved={item.get('approved')}")
        for s in issues:
            print("    -", s)
        problems += bool(issues)
    print(f"\n{len(queue['items'])} عنصر، {problems} فيه مشاكل.")
    return 1 if problems else 0


def schedulable_on_meta(item):
    """منشور فيسبوك بصورة واحدة فقط يُجدول على خوادم Meta مسبقاً."""
    return item["network"] == "facebook" and item["kind"] == "post" and len(item.get("images", [])) == 1


def actionable(item, now):
    """هل يجب أن يفعل الناشر شيئاً بهذا العنصر الآن؟"""
    if not item.get("approved"):
        return False
    if item["status"] == "failed":
        if item.get("attempts", 0) >= MAX_ATTEMPTS:
            return False
        last = item.get("last_try")
        if last and now - datetime.fromisoformat(last) < RETRY_AFTER:
            return False
    elif item["status"] != "pending":
        return False
    lead = when(item) - now
    if schedulable_on_meta(item) and FB_SCHEDULE_MIN_LEAD <= lead <= FB_SCHEDULE_MAX_LEAD:
        return True
    return lead <= timedelta(0)


def withdrawn(item):
    """منشور فيسبوك مجدول لدى Meta ثم سحب المالك موافقته (approved: false)."""
    return not item.get("approved")


def needs_unscheduling(item, now):
    """منشور فيسبوك مجدول لدى Meta وموعده لم يحن بعد، وفيه أحد أمرين: سحب المالك موافقته،
    أو كاروسيل جدوله الكود القديم (قبل 5 تشرين الأول) بصورة واحدة.
    تُحذف جدولته لدى Meta ويعود إلى الانتظار: المسحوب لا يُنشر حتى يُوافق عليه من جديد،
    والكاروسيل يُنشر كاملاً في موعده."""
    if not (item["network"] == "facebook" and item["kind"] == "post" and item["status"] == "scheduled"
            and item.get("unschedule_attempts", 0) < MAX_ATTEMPTS
            and when(item) - now > FB_SCHEDULE_MIN_LEAD):
        return False
    return withdrawn(item) or (len(item["images"]) > 1 and "fallback" in (item.get("result") or {}))


def unschedule(item, page_token):
    """يحذف المنشور المجدول بصورة واحدة ويعيد العنصر إلى الانتظار. الحذف قبل التغيير: إن فشل بقي كما هو."""
    old = item["result"]
    try:
        api("DELETE", old["id"], page_token)
    except GraphError as e:
        if "→ HTTP 4" not in str(e):
            raise
        # (#100) غير موجود: حُذف من قبل، فالمنشور المجدول لم يعد قائماً ويمكن المتابعة.
        if "does not exist" not in str(e) and "(#100)" not in str(e):
            raise
    item["superseded"] = dict(old, unscheduled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    item["status"] = "pending"
    item["attempts"] = 0
    for k in ("result", "done_at", "error", "unschedule_attempts", "unschedule_error"):
        item.pop(k, None)


def alert(title, body):
    """يفتح Issue في المستودع، فيصل بريد GitHub إلى المالك. لا يوقف النشر إن فشل."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        print("تنبيه (بلا GitHub):", title)
        return
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues", method="POST",
        data=json.dumps({"title": title, "body": body}).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    try:
        urllib.request.urlopen(req, timeout=30).close()
    except Exception as e:
        print("تعذّر فتح Issue التنبيه:", e)


def cmd_run():
    queue = load_queue()
    now = datetime.now(timezone.utc)
    due = [i for i in queue["items"] if actionable(i, now)]
    stale = [i for i in queue["items"] if needs_unscheduling(i, now)]
    if not due and not stale:
        print("لا شيء للنشر الآن.")
        return 0
    page, page_token, ig_id, user_token = connect()
    failures = 0
    for item in stale:
        was_withdrawn = withdrawn(item)
        try:
            unschedule(item, page_token)
            if was_withdrawn:
                print(f"↺ {item['id']}: سحب المالك موافقته؛ حُذفت جدولته لدى Meta ولن يُنشر")
            else:
                print(f"↺ {item['id']}: حُذفت جدولة الصورة الواحدة؛ يُنشر كاروسيلاً في موعده")
        except Exception as e:
            item["unschedule_attempts"] = item.get("unschedule_attempts", 0) + 1
            item["unschedule_error"] = str(e)[:600]
            failures += 1
            print(f"✗ {item['id']}: تعذّر حذف الجدولة: {e}")
            if item["unschedule_attempts"] >= MAX_ATTEMPTS:
                if was_withdrawn:
                    alert(f"تعذّر إلغاء جدولة {item['id']} بعد سحب الموافقة",
                          "ما زال مجدولاً لدى Meta وسيُنشر في موعده. احذفه يدوياً من Meta Business Suite "
                          f"← المنشورات المجدولة. الخطأ:\n\n```\n{item['unschedule_error']}\n```")
                else:
                    alert(f"تعذّر حذف جدولة {item['id']} بصورة واحدة",
                          f"سيُنشر في موعده بصورة واحدة كما جُدول. الخطأ:\n\n```\n{item['unschedule_error']}\n```")
        save_queue(queue)
    for item in due:
        t = when(item)
        lead = t - now
        item["last_try"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            if schedulable_on_meta(item) and lead >= FB_SCHEDULE_MIN_LEAD:
                res = fb_post(page["id"], page_token, item, schedule_ts=int(t.timestamp()))
                item["status"] = "scheduled"
            elif -lead > LATE_LIMIT:
                item["status"] = "failed"
                item["attempts"] = MAX_ATTEMPTS
                item["error"] = f"فات الموعد بأكثر من {LATE_LIMIT}؛ لم يُنشر متأخراً"
                failures += 1
                print(f"✗ {item['id']}: {item['error']}")
                alert(f"لم يُنشر {item['id']}", item["error"])
                save_queue(queue)
                continue
            elif item["network"] == "facebook" and item["kind"] == "reel":
                res = fb_reel(page["id"], page_token, item)
                item["status"] = "published"
            elif item["network"] == "facebook" and item["kind"] == "post":
                res = fb_post(page["id"], page_token, item)
                item["status"] = "published"
            elif item["network"] == "facebook":
                res = fb_story(page["id"], page_token, item)
                item["status"] = "published"
            elif item["network"] == "instagram":
                if not ig_id:
                    raise GraphError("لا يوجد حساب إنستغرام مربوط بالصفحة")
                res = ig_publish(ig_id, user_token, item)
                item["status"] = "published"
            else:
                raise ValueError(f"شبكة غير معروفة: {item['network']}")
            item["result"] = res
            item["done_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            item.pop("error", None)
            print(f"✓ {item['id']} → {item['status']} {res}")
            if "fallback" in res:
                alert(f"{item['id']} نُشر بصورة واحدة بدل الكاروسيل", res["fallback"])
        except Exception as e:
            item["status"] = "failed"
            item["attempts"] = MAX_ATTEMPTS if isinstance(e, PermanentError) else item.get("attempts", 0) + 1
            item["error"] = str(e)[:600]
            failures += 1
            print(f"✗ {item['id']}: {e}")
            if item["attempts"] >= MAX_ATTEMPTS:
                alert(f"فشل نشر {item['id']}", f"بعد {item['attempts']} محاولات:\n\n```\n{item['error']}\n```")
        save_queue(queue)
    return 1 if failures else 0


REEL_TEST = "media/test/reel-test.mp4"


def cmd_videotest():
    """اختبار رفع الريلز بلا نشر: حاوية إنستغرام تُعالَج ولا تُنشر (تنتهي بعد 24 ساعة)،
    وريلز فيسبوك يُرفع مسودة ثم يُحذف. لا يظهر شيء للمتابعين."""
    page, page_token, ig_id, user_token = connect()
    item = {"video": REEL_TEST, "caption": "اختبار رفع — لا يُنشر"}
    errors = []
    try:
        cid = ig_reel_container(ig_id, user_token, item)
        print(f"✓ إنستغرام: الحاوية {cid} جاهزة (FINISHED)، ولم تُنشر")
    except Exception as e:
        errors.append(f"إنستغرام: {e}")
        print(f"✗ إنستغرام: {e}")
    try:
        res = fb_reel(page["id"], page_token, item, state="DRAFT")
        print(f"✓ فيسبوك: رُفع الريلز مسودة {res}")
        api("DELETE", res["video_id"], page_token)
        print("✓ فيسبوك: حُذفت المسودة")
    except Exception as e:
        errors.append(f"فيسبوك: {e}")
        print(f"✗ فيسبوك: {e}")
    if errors:
        # سجل Actions لا يُقرأ بلا تسجيل دخول؛ الـIssue يُقرأ من الواجهة العامة.
        alert("فشل اختبار رفع الريلز", "\n\n".join(f"```\n{s}\n```" for s in errors))
    return 1 if errors else 0


# ---------- المناوبة ----------

def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, check=check, capture_output=True, text=True)


def sync_with_remote():
    """يجلب عناصر الطابور الجديدة ويدفع حالة النشر. يعيد True إن لم يبقَ شيء غير مدفوع."""
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    if git("pull", "--rebase", "--autostash", "-q", "origin", branch, check=False).returncode != 0:
        git("rebase", "--abort", check=False)  # تعارض نادر: نُبقي حالتنا محلياً ونحاول في الدورة التالية
        print("تعذّر سحب التحديثات؛ سأعيد المحاولة.")
        return False
    if git("rev-list", "--count", f"origin/{branch}..HEAD").stdout.strip() != "0":
        return git("push", "-q", "origin", f"HEAD:{branch}", check=False).returncode == 0
    return True


def save_status():
    git("add", "queue.json")
    if git("diff", "--cached", "--quiet", check=False).returncode != 0:
        git("commit", "-q", "-m", "Update publish status")


def cmd_loop():
    end = time.monotonic() + 60 * int(os.environ.get("LOOP_MINUTES", "330"))
    synced = sync_with_remote()
    while True:
        try:
            cmd_run()
        except Exception as e:  # خطأ غير متوقع لا يوقف المناوبة
            print("خطأ في الدورة:", e)
        save_status()
        synced = sync_with_remote()
        if time.monotonic() + LOOP_SLEEP > end:
            break
        time.sleep(LOOP_SLEEP)
    for _ in range(5):  # قبل نهاية المناوبة: لا نترك حالة نشر غير محفوظة
        if synced:
            return 0
        time.sleep(20)
        synced = sync_with_remote()
    alert("تعذّر حفظ حالة النشر في المستودع",
          "قد تُعاد محاولة نشر عنصر نُشر فعلاً. راجع آخر تشغيل في Actions.")
    return 1


if __name__ == "__main__":
    commands = {"check": cmd_check, "run": cmd_run, "whoami": cmd_whoami, "loop": cmd_loop,
                "videotest": cmd_videotest}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        print(__doc__)
        sys.exit(2)
    sys.exit(commands[sys.argv[1]]() or 0)
