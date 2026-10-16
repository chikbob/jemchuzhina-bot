# Логотип «Жемчужины»

Исходник, предоставленный владельцем проекта: `logo.jpg`. Новый логотип создан встроенным инструментом imagegen на его основе; исходный файл сохранён. Финальный PNG: [logo.png](../logo.png), версия для сайта: [pearl/static/brand/logo.png](../pearl/static/brand/logo.png), JPEG для Telegram: [avatar.jpg](../pearl/static/brand/avatar.jpg).

Сохранены фронтальный женский портрет, тёмные волосы с бирюзовыми и красными прядями, ромбовидные серьги, солнце и геометрическая цветовая палитра исходного знака. Упрощены мелкие детали для небольшого аватара. Текст не добавлялся.

## Финальный промпт

> Use case: logo-brand / style-transfer. Reference image: supplied existing logo of the beauty academy Жемчужина. Create one polished square 1024x1024 logo/avatar adapted from this exact identity. Preserve its recognizable front-facing geometric woman's portrait, dark navy flowing hair with turquoise and red strands, pale cream face, confident symmetrical eyes, red lips, diamond-shaped earrings and the small sun/star above her head. Preserve the original turquoise, navy, crimson, orange and warm gold palette. Refine the geometry into cleaner, more consistent flat shapes, reduce tiny busy facets so the face is legible at 48px. Center the face with a balanced simple geometric halo inspired by the reference. Telegram circle crop safe: all essential portrait, earrings and sun fit comfortably within central 78% of the square; leave generous white/ivory outer negative space. White background. Crisp vector-like edges, tasteful professional beauty-academy identity. No text, no lettering, no watermark, no added objects, no photorealism. Return only the finished logo, not a mockup or presentation sheet.

Для обновления описаний и аватара без изменения webhook:

```bash
.venv/bin/python scripts/sync_branding.py
```
