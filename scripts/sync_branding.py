"""Apply academy descriptions and avatar without changing the webhook."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram.types import FSInputFile, InputProfilePhotoStatic

from pearl.brand import BOT_DESCRIPTION, BOT_SHORT_DESCRIPTION
from pearl.config import Config
from pearl.transport import create_bot


async def main():
    bot = create_bot(Config.load())
    bot.session.timeout = 90
    try:
        actions = {
            "description": bot.set_my_description(BOT_DESCRIPTION),
            "short_description": bot.set_my_short_description(BOT_SHORT_DESCRIPTION),
            "avatar": bot.set_my_profile_photo(
                InputProfilePhotoStatic(
                    photo=FSInputFile(
                        Path(__file__).resolve().parent.parent
                        / "pearl/static/brand/avatar.jpg"
                    )
                )
            ),
        }
        results = await asyncio.gather(*actions.values(), return_exceptions=True)
        for name, result in zip(actions, results, strict=True):
            print(name + ": " + ("ok" if result is True else type(result).__name__))
        if any(result is not True for result in results):
            raise SystemExit(1)
        description = await bot.get_my_description()
        short = await bot.get_my_short_description()
        if (
            description.description != BOT_DESCRIPTION
            or short.short_description != BOT_SHORT_DESCRIPTION
        ):
            raise SystemExit("Description verification failed")
        print("Branding verified")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
