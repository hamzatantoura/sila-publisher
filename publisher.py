"""ناشر صلة — ينشر منشورات queue.json على صفحة فيسبوك وحساب إنستغرام عبر Meta Graph API.

الأوامر:
  python publisher.py check    فحص الطابور والصور بلا اتصال بـ Meta
  python publisher.py whoami   فحص المفتاح: اسم الصفحة وحساب إنستغرام وحصة النشر
  python publisher.py run      نشر أو جدولة ما حان وقته (يشغّله GitHub Actions كل 15 دقيقة)

متغيرات البيئة:
  META_TOKEN      مفتاح مستخدم النظام (سرّي، يُحفظ في GitHub Secrets فقط)
  PAGE_ID         معرّف صفحة فيسبوك
  MEDIA_BASE_URL  الرابط العام لجذر المستودع، تُبنى منه روابط الصور
  GRAPH_VERSION   اختياري، الافتراضي v26.0

لا يعتمد إلا على مكتبة بايثون القياسية، حتى يعمل على GitHub Actions بلا تثبيت.
"""
import json
import os
import struct
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
LATE_LIMIT = timedelta(hours=3)
MAX_ATTEMPTS = 3


class GraphError(Exception):
    pass


class PermanentError(Exception):
    """خطأ لن تحلّه إعادة المحاولة: يُعلَّم العنصر فاشلاً نهائياً من أول مرة."""


def api(method, path, token, **params):
    params["access_token"] = token
    body = urllib.parse.urlencode(params)
    if method == "GET":
        req = urllib.request.Request(f"{GRAPH}/{path}?{body}")
    else:
        req = urllib.request.Request(f"{GRAPH}/{path}", data=body.encode(), method="POST")
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


# ---------- فيسبوك ----------

def fb_upload_photo(page_id, token, path, **extra):
    return api("POST", f"{page_id}/photos", token, url=media_url(path), published="false", **extra)["id"]


def fb_post(page_id, token, item, schedule_ts=None):
    sched = {}
    if schedule_ts:
        sched = {"published": "false", "scheduled_publish_time": str(schedule_ts)}
    images = item["images"]
    if len(images) == 1:
        params = {"url": media_url(images[0]), "message": item["caption"]}
        params.update(sched)
        return api("POST", f"{page_id}/photos", token, **params)
    # منشور متعدد الصور: ترفع الصور غير منشورة ثم تُرفق بمنشور واحد.
    extra = {"temporary": "true"} if schedule_ts else {}
    try:
        ids = [fb_upload_photo(page_id, token, p, **extra) for p in images]
    except GraphError as e:
        # تطبيق في وضع التطوير لا يُسمح له بصور غير منشورة: (#200) … create an unpublished post.
        # البديل: الشريحة الأولى منشوراً واحداً مع النص كاملاً (الكاروسيل يبقى كاملاً على إنستغرام).
        if "(#200)" not in str(e) or "unpublished" not in str(e):
            raise
        single = dict(item, images=images[:1])
        res = fb_post(page_id, token, single, schedule_ts)
        res["fallback"] = "single-image: unpublished photos not permitted"
        return res
    params = {
        "message": item["caption"],
        "attached_media": json.dumps([{"media_fbid": i} for i in ids]),
    }
    params.update(sched)
    return api("POST", f"{page_id}/feed", token, **params)


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

def ig_wait(container_id, token):
    for _ in range(30):
        status = api("GET", container_id, token, fields="status_code").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise GraphError(f"حاوية إنستغرام {container_id} حالتها {status}")
        time.sleep(5)
    raise GraphError(f"حاوية إنستغرام {container_id} لم تجهز خلال دقيقتين ونصف")


def ig_publish(ig_id, token, item):
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
        cap = item.get("caption", "")
        if item["kind"] == "post" and not cap:
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


def cmd_run():
    queue = load_queue()
    now = datetime.now(timezone.utc)
    due = [i for i in queue["items"] if i.get("approved") and (
        i["status"] == "pending" or (i["status"] == "failed" and i.get("attempts", 0) < MAX_ATTEMPTS))]
    if not due:
        print("لا شيء للنشر الآن.")
        return 0
    page, page_token, ig_id, user_token = connect()
    failures = 0
    for item in due:
        t = when(item)
        lead = t - now
        try:
            if item["network"] == "facebook" and item["kind"] == "post" and lead >= FB_SCHEDULE_MIN_LEAD:
                if lead > FB_SCHEDULE_MAX_LEAD:
                    continue
                res = fb_post(page["id"], page_token, item, schedule_ts=int(t.timestamp()))
                item["status"] = "scheduled"
            elif lead > timedelta(0):
                continue
            elif -lead > LATE_LIMIT:
                item["status"] = "failed"
                item["attempts"] = MAX_ATTEMPTS
                item["error"] = f"فات الموعد بأكثر من {LATE_LIMIT}؛ لم يُنشر متأخراً"
                failures += 1
                print(f"✗ {item['id']}: {item['error']}")
                save_queue(queue)
                continue
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
        except Exception as e:
            item["status"] = "failed"
            item["attempts"] = MAX_ATTEMPTS if isinstance(e, PermanentError) else item.get("attempts", 0) + 1
            item["error"] = str(e)[:600]
            failures += 1
            print(f"✗ {item['id']}: {e}")
        save_queue(queue)
    return 1 if failures else 0


if __name__ == "__main__":
    commands = {"check": cmd_check, "run": cmd_run, "whoami": cmd_whoami}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        print(__doc__)
        sys.exit(2)
    sys.exit(commands[sys.argv[1]]() or 0)
