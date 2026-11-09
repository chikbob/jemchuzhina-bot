# Обложка для портфолио

Файл: [cover-portfolio.png](assets/cover-portfolio.png). Создан встроенным image_gen с помощью навыка imagegen; затем выполнена одна правка надписи в строке браузера. Это презентационная композиция на основе интерфейсов, а не прямой скриншот.

Референсы: обложка проекта bulka-application (стиль и композиция), предоставленный image.png (Telegram), [демонстрационный кабинет](assets/dashboard.jpg), [логотип](../logo.png). Исходный image.png сохранён без изменений. Старые cover.png и cover.svg сохранены.

## Промпт генерации

```text
Use case: compositing / ads-marketing.
Asset: polished GitHub README portfolio cover for beauty academy Telegram booking project Жемчужина, landscape approximately 2400x1680, same editorial product showcase family as reference 1.
Inputs: image 1 /bulka-application/docs/assets/cover.webp is STYLE AND COMPOSITION reference only: warm ivory background, huge bold headline upper left, small brand header, large lower desktop app frame and overlapping slightly tilted phone on left, soft realistic shadows, generous spacing, small footer metadata. Do not reproduce Bulka food, red identity, or wording.
Image 2 /jemchuzhena_bot/image.png is the actual Telegram screenshot to incorporate: preserve dark Telegram UI, bot name, woman's avatar, introduction bubble with haircut/color/training information. Adapt it by cropping the main chat and intro card into a tall dark smartphone screen lower left. Omit right profile sidebar, report/block buttons; do not make those the hero. Faithful screenshot compositing; do not invent new bot functionality or messages.
Image 3 /jemchuzhena_bot/docs/assets/dashboard.jpg is the real web owner dashboard with fake demo data, place it in a broad desktop browser frame occupying lower right, large and crisp; preserve sidebar and dashboard appearance and text, do not redesign the application.
Image 4 /jemchuzhena_bot/logo.png is actual brand mark. Preserve its recognizable multicolored geometric woman's face and use as small logo next to Жемчужина at top left and a larger circular mark at top right as understated counterpart to the Bulka asterisk.
Palette: warm ivory #f2efe6 background, deep navy #173c45 headline, muted teal #397e81 accents, soft mint halo behind larger logo. Keep rainbow colors limited to original brand mark.
Exact major copy upper left on two lines: "Запись —" then "без ожидания." Large premium heavy sans Cyrillic, perfectly spelled and legible. Small subtitle below: "Стрижка, окрашивание и обучение — в Telegram." Top left brand "Жемчужина", top right small label "TELEGRAM-БОТ ДЛЯ АКАДЕМИИ". Small stack labels above desktop: "PYTHON  ·  AIOGRAM  ·  SQLITE  ·  FLASK". Bottom left "ЖЕМЧУЖИНА / МАРИУПОЛЬ". Bottom right "ЗАПИСЬ · РАСПИСАНИЕ · КОМАНДА".
Composition: headline upper ~35%, product screens lower ~60%, phone left about22% width, desktop right about70% width, slight overlap, no clipping headline. Product design case study, calm refined spacing, no salon stock photos, no extra props, no watermarks. Return finished flat cover image only.
```

## Финальная правка

```text
Use case: precise-object-edit / text-localization. Edit only the tiny text centered in the desktop browser top toolbar: replace the invented URL "academy.zhemchuzhina.app" with the exact text "Веб-кабинет · Демо". Preserve absolutely everything else in the image unchanged: layout, Cyrillic headlines, colors, original logo, phone, dashboard, shadows, footer, dimensions. Do not add any URLs. Return same finished landscape cover.
```

