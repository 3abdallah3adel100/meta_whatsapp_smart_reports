# إضافة Overall Live Budget — 11 نوفمبر 2026

## الخلاصة

- `Overall Daily Budget`: بدون أي تغيير. يجمع ميزانيات الـ CBO/ABO الحالية للمجموعات التي فيها إعلانات ACTIVE، **بالإضافة إلى مصروف اليوم للمجموعات التي توقفت**، وفق قواعد closing الحالية.
- `Overall Live Budget`: يجمع الميزانيات اليومية الحالية للـ CBO/ABO التي لديها إعلان ACTIVE فعليًا فقط. لا يدخل فيه أي مصروف تم قبل الإيقاف.
- لا يتم تكرار ميزانية CBO عند وجود عدة Ad Sets/Ads.
- تم إضافة السطر تحت إجمالي الـ Daily Budget في تقرير Balance للعميل/الوكيل، وكذلك في تقارير Overall Allocation وCairo Overall.
- باقي الشغل، ومنها تقرير أرقام الواتساب، بدون تعديل.

## الملفات المعدلة

1. `allocation_meta.py`: إضافة `live_daily_budget` إلى Snapshot الحساب، بالاعتماد على بند `calculation_mode == 'live'` الموجود مسبقًا في `budget_details`.
2. `smart_report.py`: إظهار إجمالي `Overall Live Budget` بعد إجمالي الـ Daily Budget.
3. `tests/test_overall_live_budget.py`: اختبارات للميزانية الحية، والصرف للمجموعات المتوقفة، وCBO، وتنسيق التقارير.

## تطبيق التعديل على GitHub

إذا هترفع مشروع ZIP كامل، استبدل الملفات الموجودة بنفس أسمائها ومساراتها. لو عايز أقل تغيير، ارفع ملفي `allocation_meta.py` و`smart_report.py` فقط من باكدج `Overall_Live_Budget_Changed_Files.zip` على جذر الريبو.

بعد الاستبدال، شغّل GitHub Actions كالعادة.

## تحقق

الاختبارات: `python -m unittest discover -s tests -v`
نتيجة النسخة الحالية: 29 اختبار ناجح (اختبارات محلية بدون بيانات Meta حية).
