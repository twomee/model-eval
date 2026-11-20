# evalkit: eval אישי להשוואת מודלים

כלי CLI שמריץ **את המשימות שלך** על כמה מודלים מכמה חברות, מציין את התשובות ומפיק דוח שעוזר להחליט.
הוא עובד גם בלי מפתחות API: דרך ה-CLI הרשמיים שרצים על המנוי, או דרך העתק-הדבק.

## התחלה מהירה

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. dry run בלי עלות. מוודא שכל הצנרת עובדת (התוצאה הצפויה: good > sloppy > broken)
python -m evalkit run --config config.mock.yaml --run mock
open results/mock/report.html

# 2. בדיקת config ומשימות, בלי לקרוא לאף מודל
python -m evalkit validate

# 3. לוודא שכל CLI עובד לבד, לפני שמריצים את כולם
echo "say hi" | claude -p --output-format json
codex exec --skip-git-repo-check "say hi"
gemini -p "say hi"

# 4. הרצה קטנה קודם (משימה אחת), ורק אחר כך הכל
python -m evalkit run --run smoke --tasks gen_capacity_estimate
python -m evalkit run --run v1
```

### מודלים ידניים (Mistral Le Chat, GLM)
```bash
python -m evalkit export            # יוצר manual/<model>/<task>.prompt.md
# מדביקים כל prompt בצ'אט, ושומרים את התשובה ב-<task>.answer.md באותה תיקייה
python -m evalkit run --run v1      # התשובות שנשמרו נאספות ומצוינות, החסרות מדולגות
```
טיפ: לפתוח שיחה חדשה לכל prompt, כדי שההקשר מהשאלות הקודמות לא ישפיע על התשובה.

### פקודות שימושיות
| פקודה | מה עושה |
|---|---|
| `run --models claude,gemini` | רק חלק מהמודלים |
| `run --category backend_code` | רק קטגוריה אחת |
| `grade --run v1 --regrade` | ציון מחדש בלי לייצר תשובות מחדש (אחרי שינוי rubric) |
| `report --run v1` | הפקת הדוח מחדש |

## איך זה בנוי

```
config.yaml          מי מתחרה, איך פונים אליו, ומשקל כל קטגוריה בשבילך
tasks/*.yaml         מה בודקים: prompt, reference ו-graders
evalkit/
  providers.py       Adapter לכל ספק מאחורי ממשק אחד: complete(Request) -> Completion
                     anthropic | openai_compat | cli | manual | mock
  graders.py         python_tests (הרצת טסטים) | match (regex) | length (אילוץ) | rubric (LLM-as-judge)
  runner.py          generate -> generations.jsonl, grade -> grades.jsonl  (שני שלבים, resumable)
  report.py          ציון משוקלל, CI ב-bootstrap, latency, עלות, ו-drill-down לכל תשובה
```

**החלטות תכנון ולמה התקבלו**
- **ממשק Provider צר.** כשעוברים מ-CLI ל-API משנים שורה ב-config, לא את הקוד. כל ספק חדש הוא class אחד.
- **Generate ו-Grade בנפרד.** אחרי שתראה תשובות אמיתיות, תרצה לשנות rubrics. התשובות הגולמיות שמורות, כך שציון מחדש לא עולה כלום.
- **Idempotent ו-resumable.** כל תוצאה נשמרת לפי מפתח `model|task|fingerprint|rep`. אם ההרצה נפלה באמצע, מריצים שוב והיא ממשיכה מאיפה שעצרה. כשמשנים משימה, ה-fingerprint שלה משתנה, ותוצאות ישנות לא נספרות בטעות.
- **אילוצים מוכפלים ולא ממוצעים.** תשובה ריקה "עומדת" באילוץ אורך. אם האורך היה נכנס לממוצע, היא הייתה מקבלת נקודות בחינם. התגלה בהרצת ה-mock.
- **ממוצע של קטגוריות ולא של משימות.** אחרת הקטגוריה שכתבת לה הכי הרבה משימות שולטת בתוצאה בלי שתשים לב.

## איך לקרוא את הדוח
- **CI 90%:** אם הטווחים של שני מודלים חופפים ברובם, ה-eval עדיין לא מבחין ביניהם. במקרה כזה מוסיפים משימות ולא מכריזים על מנצח.
- **Inconsistency:** סטיית התקן בין החזרות. מודל עם ציון ממוצע קצת נמוך יותר אבל צפוי יותר יכול להיות בחירה טובה יותר.
- **Judge spread** (ב-drill-down): פער גדול בין השופטים בדרך כלל אומר שה-rubric עמום, ולא שהתשובה גבולית.
- **תמיד לקרוא תשובות בעצמך.** הציון הוא כלי לסינון, לא האמת עצמה.

## מגבלות שחשוב להכיר
1. **CLI הוא לא מודל נקי.** Claude Code, Codex ו-Gemini CLI מוסיפים system prompt משלהם ואולי בוחרים מודל בעצמם. Claude Code גם טוען את `~/.claude/CLAUDE.md` שלך. מה שנמדד הוא "הכלי כמו שאתה משתמש בו". זה לגיטימי, אבל צריך לדעת את זה. כדאי לבדוק ב-`--help` של כל CLI את הדגלים לבחירת מודל ולביטול הגדרות משתמש.
2. **Self-preference bias.** שופט נוטה להעדיף תשובות של המודל שלו. לכן יש פאנל של שני שופטים מספקים שונים. `validate` מתריע כשאותו מודל גם מתחרה וגם שופט.
3. **קוד שהמודל כתב רץ אצלך.** הוא מוגבל ב-timeout בתוך subprocess. זה מגן מתאונות, לא מכוונה רעה. כשבודקים מודלים לא מוכרים, מריצים בתוך Docker.
4. **Rate limits של מנויים.** ברירת המחדל היא concurrency=1 לכל CLI. כשמגיעים למגבלה מחכים ומריצים שוב, וה-resume ממשיך מאותה נקודה.
5. **משימות ציבוריות מזוהמות.** אם המשימה מופיעה באינטרנט, ייתכן שהמודל ראה אותה באימון. משימות מהעבודה שלך מגינות מזה.

## הצעד החשוב ביותר: להחליף את המשימות שלי בשלך
11 המשימות כאן הן דוגמאות שמראות כל סוג grader. eval אמין צריך **15–30 משימות מהעבודה האמיתית שלך**:
- באג אמיתי שתיקנת החודש, עם הטסט שתופס אותו.
- שאלת design שהתלבטת בה, עם ה-trade-offs שבסוף הכריעו.
- מייל או מסמך בעברית שבאמת שלחת.

**הכלל:** משימה טובה היא כזו שמודל חלש נכשל בה. אם כל המודלים מקבלים 1.0, היא לא תורמת כלום להחלטה.

### תבנית למשימה חדשה
```yaml
- id: my_task
  category: backend_code          # או קטגוריה חדשה, ואז להוסיף לה משקל ב-config
  prompt: |
    ...
  reference: |                    # אופציונלי: מראה לשופט איך נראית תשובה טובה
    ...
  graders:
    - type: python_tests          # או match / length / rubric
      weight: 2
      tests: |
        from solution import ...
    - type: rubric
      criteria:
        - id: correctness
          weight: 2
          description: התנהגות שאפשר לראות בתשובה ("מציין מה היה מודד קודם"), לא תחושה ("תובנתי")
```
