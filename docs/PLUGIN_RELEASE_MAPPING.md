# خريطة إصدارات Gumacu ChatGPT Plugin ومطابقتها مع خادم MCP

توثق هذه الوثيقة العلاقة بين إصدار **ChatGPT Plugin (UI)** وإصدار **خادم MCP على Netcup**، بالإضافة إلى دورة الإصدار المعتمدة مستقبلاً.

---

## 1. جدول مطابقة الإصدارات (Release Mapping Matrix)

| تاريخ الإصدار | إصدار الـ Plugin (ChatGPT) | إصدار الـ MCP (Netcup) | حالة التحقق (`agy_doctor`) | Release ID في منصة OpenAI | ملاحظات |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 2026-10-13 | `1.0.0` | `0.1.9` | نعم | `PluginRelease_01b0ecec882c81919e438342800e931f` | الإصدار المبدئي للـ Plugin |
| 2026-10-14 | `1.0.1` | `0.1.10` | نعم (`healthy: true`) | قيد النشر عبر Plugin Creator | تحديث الإصدار ودعم إعلان النسخة الحية |

---

## 2. آلية وتدفق الإصدارات المعتمدة (Release Workflow)

تتبع عملية التحديث دائماً الخطوات المتسلسلة التالية:

```
[1. تطوير الميزة أو الإصلاح في الكود]
                     │
                     ▼
[2. رفع رقم إصدار MCP في pyproject.toml و __init__.py]
                     │
                     ▼
[3. تشغيل الاختبارات المحلية والفحص]
                     │
                     ▼
[4. نشر الكود الجديد على سيرفر Netcup وتثبيته في venv]
                     │
                     ▼
[5. إعادة تحميل عملية MCP / Tunnel بأمان دون لمس الخدمات الإنتاجية]
                     │
                     ▼
[6. فحص الاتصال الفعلي والتأكد من نجاح agy_doctor مطابقة الإصدار]
                     │
                     ├─── إذا فشل ──> (توقف ولا ترفع إصدار الـ Plugin)
                     │
                     ▼ إذا نجح
[7. تشغيل سكريبت تجهيز الـ Plugin: scripts/package_plugin_release.py]
                     │
                     ▼
[8. تطبيق التحديث على Plugin Creator داخل ChatGPT ورفع ZIP]
                     │
                     ▼
[9. التحقق من ظهور الإصدار الجديد في صفحة Plugins في المتصفح]
```

---

## 3. أدوات الأتمتة المتاحة

تم إنشاء أداة مخصصة لفحص السيرفر وتجهيز حزم الإصدار تلقائياً:

```bash
# تشغيل الفحص والتجهيز التلقائي للإصدار:
python scripts/package_plugin_release.py --plugin-version 1.0.1 --verify-remote
```

تقوم الأداة بـ:
1. قراءة إصدار MCP الحالي من `pyproject.toml`.
2. الاتصال بسيرفر Netcup عبر SSH وتنفيذ `agy_doctor` للتأكد من الصحة ومطابقة الإصدار.
3. تحديث ملفات التكوين (`.app.json`, `plugin.json`, `.codex-plugin/plugin.json`).
4. تحديث الكاش المحلي في `~/.codex/plugins/cache/created-by-me-remote/dev-6acf79599a7c8191a7eb5b52b4d30c51`.
5. توليد ملف أرشيف نظيف وجاهز للنشر: `dist/netcup-plugin-<version>.zip`.

---

## 4. بيانات الهوية الثابتة (Identity Contract)

* **Plugin ID:** `plugin_asdk_app_6acf79599a7c8191a7eb5b52b4d30c51`
* **App ID:** `asdk_app_6acf79599a7c8191a7eb5b52b4d30c51`
* **Internal Name:** `dev-6acf79599a7c8191a7eb5b52b4d30c51`
* **Tunnel ID:** `tunnel_6ace3926c4e48191aeced4035a6997e5`
