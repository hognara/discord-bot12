import discord
import os
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from discord.ext import commands
from discord import app_commands
from typing import Optional

# ============================================================
# НАСТРОЙКИ
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")

# Если хочешь, чтобы войсы создавались в определённой категории,
# укажи ID категории.
# Если оставить 0, бот сам создаст категорию "Сборы".
VOICE_CATEGORY_ID = 0

# Название категории, если VOICE_CATEGORY_ID = 0
VOICE_CATEGORY_NAME = "Сборы"

# ============================================================
# INTENTS
# ============================================================

intents = discord.Intents.default()

# Нужно для работы с участниками и голосовыми каналами
intents.members = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# ============================================================
# ХРАНЕНИЕ СБОРОВ
# ============================================================

# Формат:
#
# gatherings[gathering_id] = {
#     "guild_id": ID сервера,
#     "channel_id": ID текстового канала,
#     "message_id": ID сообщения,
#     "name": "Название",
#     "max_players": 5,
#     "role_id": ID роли или None,
#     "time": "21:00",
#     "participants": [ID, ID, ID],
#     "started": False,
#     "voice_id": None
# }

gatherings = {}

next_gathering_id = 1


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def get_gathering(gathering_id: int):
    return gatherings.get(gathering_id)


def get_role(guild: discord.Guild, role_id: Optional[int]):
    if role_id is None:
        return None

    return guild.get_role(role_id)


def build_gathering_embed(
    gathering: dict,
    guild: discord.Guild
):
    participants = gathering["participants"]
    max_players = gathering["max_players"]

    role = get_role(
        guild,
        gathering["role_id"]
    )

    if role:
        role_text = role.mention
    else:
        role_text = "Не указана"

    participant_text = ""

    if participants:
        lines = []

        for i, user_id in enumerate(participants, start=1):
            member = guild.get_member(user_id)

            if member:
                lines.append(
                    f"`{i}.` {member.mention}"
                )
            else:
                lines.append(
                    f"`{i}.` <@{user_id}>"
                )

        participant_text = "\n".join(lines)
    else:
        participant_text = "Пока никто не записался."

    if len(participants) >= max_players:
        status = "🟢 **Игроки набраны! Можно начинать.**"
    else:
        status = (
            f"🟡 **Нужно ещё "
            f"{max_players - len(participants)} игрок(а/ов).**"
        )

    embed = discord.Embed(
        title=f"🎮 {gathering['name']}",
        description=(
            "Нажмите **«Участвовать»**, чтобы попасть "
            "в список игроков.\n\n"
            f"{status}"
        ),
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="👥 Игроки",
        value=f"**{len(participants)} / {max_players}**",
        inline=True
    )

    embed.add_field(
        name="🏷 Тег участников",
        value=role_text,
        inline=True
    )

    embed.add_field(
        name="⏰ Время",
        value=gathering["time"],
        inline=True
    )

    embed.add_field(
        name="📋 Участники",
        value=participant_text,
        inline=False
    )

    embed.set_footer(
        text=f"Сбор №{gathering['id']}"
    )

    return embed


def build_gathering_view(gathering_id: int):
    gathering = gatherings[gathering_id]

    view = GatheringView(
        gathering_id=gathering_id,
        started=gathering["started"],
        full=(
            len(gathering["participants"])
            >= gathering["max_players"]
        )
    )

    return view


async def update_gathering_message(
    gathering_id: int
):
    gathering = gatherings.get(gathering_id)

    if not gathering:
        return

    guild = bot.get_guild(
        gathering["guild_id"]
    )

    if not guild:
        return

    channel = guild.get_channel(
        gathering["channel_id"]
    )

    if not channel:
        return

    try:
        message = await channel.fetch_message(
            gathering["message_id"]
        )
    except discord.NotFound:
        return
    except discord.Forbidden:
        return

    embed = build_gathering_embed(
        gathering,
        guild
    )

    view = build_gathering_view(
        gathering_id
    )

    try:
        await message.edit(
            embed=embed,
            view=view
        )
    except discord.NotFound:
        pass


async def get_voice_category(
    guild: discord.Guild
):
    # Если указана существующая категория
    if VOICE_CATEGORY_ID != 0:
        category = guild.get_channel(
            VOICE_CATEGORY_ID
        )

        if isinstance(
            category,
            discord.CategoryChannel
        ):
            return category

    # Иначе ищем категорию "Сборы"
    for category in guild.categories:
        if category.name == VOICE_CATEGORY_NAME:
            return category

    # Если её нет — создаём
    try:
        category = await guild.create_category(
            VOICE_CATEGORY_NAME,
            reason="Создание категории для сборов"
        )

        return category

    except discord.Forbidden:
        return None


# ============================================================
# MODAL — СОЗДАНИЕ СБОРА
# ============================================================

class GatheringModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(
            title="🎮 Создание сбора"
        )

        self.event_name = discord.ui.TextInput(
            label="Название события",
            placeholder="Например: Игра в CS2",
            required=True,
            max_length=100
        )

        self.players_count = discord.ui.TextInput(
            label="Количество игроков",
            placeholder="Например: 5",
            required=True,
            max_length=3
        )

        self.role = discord.ui.TextInput(
            label="Тег участников",
            placeholder="ID роли или @роль",
            required=False,
            max_length=100
        )

        self.event_time = discord.ui.TextInput(
            label="Время",
            placeholder="Например: 21:00",
            required=True,
            max_length=30
        )

        self.add_item(self.event_name)
        self.add_item(self.players_count)
        self.add_item(self.role)
        self.add_item(self.event_time)

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):
        global next_gathering_id

        # ----------------------------------------------------
        # Проверяем количество игроков
        # ----------------------------------------------------

        try:
            players_count = int(
                self.players_count.value
            )
        except ValueError:
            await interaction.response.send_message(
                "❌ Количество игроков должно быть числом.",
                ephemeral=True
            )
            return

        if players_count < 1:
            await interaction.response.send_message(
                "❌ Количество игроков должно быть больше 0.",
                ephemeral=True
            )
            return

        if players_count > 99:
            await interaction.response.send_message(
                "❌ Максимальное количество игроков — 99.",
                ephemeral=True
            )
            return

        # ----------------------------------------------------
        # Получаем роль
        # ----------------------------------------------------

        role_id = None
        role_text = self.role.value.strip()

        if role_text:
            # Если пользователь написал <@&123>
            cleaned = (
                role_text
                .replace("<@&", "")
                .replace(">", "")
                .strip()
            )

            try:
                possible_role_id = int(cleaned)
            except ValueError:
                await interaction.response.send_message(
                    "❌ Тег роли указан неправильно.\n\n"
                    "Укажи **ID роли** или вставь упоминание роли.",
                    ephemeral=True
                )
                return

            role = interaction.guild.get_role(
                possible_role_id
            )

            if not role:
                await interaction.response.send_message(
                    "❌ Я не нашёл такую роль на сервере.",
                    ephemeral=True
                )
                return

            role_id = role.id

        # ----------------------------------------------------
        # Создаём сбор
        # ----------------------------------------------------

        gathering_id = next_gathering_id
        next_gathering_id += 1

        gathering = {
            "id": gathering_id,
            "guild_id": interaction.guild.id,
            "channel_id": interaction.channel.id,
            "message_id": 0,
            "name": self.event_name.value,
            "max_players": players_count,
            "role_id": role_id,
            "time": self.event_time.value,
            "participants": [],
            "started": False,
            "voice_id": None,
            "creator_id": interaction.user.id,
            "full_announced": False
        }

        gatherings[gathering_id] = gathering

        # ----------------------------------------------------
        # Создаём сообщение
        # ----------------------------------------------------

        embed = build_gathering_embed(
            gathering,
            interaction.guild
        )

        view = build_gathering_view(
            gathering_id
        )

        await interaction.response.send_message(
            embed=embed,
            view=view
        )

        message = await interaction.original_response()

        gathering["message_id"] = message.id

        # ----------------------------------------------------
        # Если указана роль — упоминаем её
        # ----------------------------------------------------

        if role_id:
            role = interaction.guild.get_role(
                role_id
            )

            if role:
                try:
                    await message.edit(
                        content=role.mention,
                        embed=embed,
                        view=view
                    )
                except discord.Forbidden:
                    pass


# ============================================================
# КНОПКА СОЗДАНИЯ СБОРА
# ============================================================

class CreateGatheringView(
    discord.ui.View
):
    def __init__(self):
        super().__init__(
            timeout=300
        )

    @discord.ui.button(
        label="🎮 Создать сбор",
        style=discord.ButtonStyle.primary
    )
    async def create_gathering(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.send_modal(
            GatheringModal()
        )


# ============================================================
# КНОПКИ САМОГО СБОРА
# ============================================================

class GatheringView(
    discord.ui.View
):
    def __init__(
        self,
        gathering_id: int,
        started: bool,
        full: bool
    ):
        super().__init__(
            timeout=None
        )

        self.gathering_id = gathering_id

        # ----------------------------------------------------
        # Кнопка участвовать
        # ----------------------------------------------------

        participate_button = discord.ui.Button(
            label="Участвовать",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"gathering_join_{gathering_id}"
        )

        participate_button.callback = (
            self.join_callback
        )

        self.add_item(
            participate_button
        )

        # ----------------------------------------------------
        # Кнопка выйти
        # ----------------------------------------------------

        leave_button = discord.ui.Button(
            label="Выйти",
            emoji="🚪",
            style=discord.ButtonStyle.secondary,
            custom_id=f"gathering_leave_{gathering_id}"
        )

        leave_button.callback = (
            self.leave_callback
        )

        self.add_item(
            leave_button
        )

        # ----------------------------------------------------
        # Кнопка начать
        # ----------------------------------------------------

        start_button = discord.ui.Button(
            label="Начать",
            emoji="🚀",
            style=discord.ButtonStyle.primary,
            custom_id=f"gathering_start_{gathering_id}",
            disabled=(
                started or not full
            )
        )

        start_button.callback = (
            self.start_callback
        )

        self.add_item(
            start_button
        )

    # ========================================================
    # УЧАСТВОВАТЬ
    # ========================================================

    async def join_callback(
        self,
        interaction: discord.Interaction
    ):
        gathering = gatherings.get(
            self.gathering_id
        )

        if not gathering:
            await interaction.response.send_message(
                "❌ Этот сбор больше не существует.",
                ephemeral=True
            )
            return

        if gathering["started"]:
            await interaction.response.send_message(
                "❌ Сбор уже начался.",
                ephemeral=True
            )
            return

        participants = gathering["participants"]

        # Уже записан
        if interaction.user.id in participants:
            await interaction.response.send_message(
                "⚠️ Ты уже участвуешь в этом сборе.",
                ephemeral=True
            )
            return

        # Уже заполнено
        if len(participants) >= gathering["max_players"]:
            await interaction.response.send_message(
                "❌ В этом сборе уже набрано нужное "
                "количество игроков.",
                ephemeral=True
            )
            return

        # ----------------------------------------------------
        # Проверяем роль
        # ----------------------------------------------------

        role_id = gathering["role_id"]

        if role_id:
            role = interaction.guild.get_role(
                role_id
            )

            if role:
                member = interaction.guild.get_member(
                    interaction.user.id
                )

                if member and role not in member.roles:
                    await interaction.response.send_message(
                        f"❌ Ты должен иметь роль {role.mention}, "
                        "чтобы участвовать.",
                        ephemeral=True
                    )
                    return

        # ----------------------------------------------------
        # Добавляем ID
        # ----------------------------------------------------

        participants.append(
            interaction.user.id
        )

        # ----------------------------------------------------
        # Если все игроки набраны — тегаем ВСЕХ участников
        # ----------------------------------------------------

        if (
            len(participants) >= gathering["max_players"]
            and not gathering.get("full_announced", False)
        ):
            gathering["full_announced"] = True

            mentions = " ".join(
                f"<@{user_id}>"
                for user_id in participants
            )

            await interaction.response.send_message(
                "🎉 **Ты последний игрок! Сбор полностью набран.**",
                ephemeral=True
            )

            try:
                await interaction.channel.send(
                    f"🎉 **СБОР НАБРАН!**\n\n"
                    f"{mentions}\n\n"
                    f"👥 Все **{len(participants)}/{gathering['max_players']}** "
                    f"игрока собрались!\n"
                    f"🔊 **Можете заходить и начинать!**",
                    allowed_mentions=discord.AllowedMentions(
                        users=True
                    )
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        else:
            await interaction.response.send_message(
                "✅ Ты успешно записался в сбор!",
                ephemeral=True
            )

        await update_gathering_message(
            self.gathering_id
        )

    # ========================================================
    # ВЫЙТИ
    # ========================================================

    async def leave_callback(
        self,
        interaction: discord.Interaction
    ):
        gathering = gatherings.get(
            self.gathering_id
        )

        if not gathering:
            await interaction.response.send_message(
                "❌ Этот сбор больше не существует.",
                ephemeral=True
            )
            return

        if gathering["started"]:
            await interaction.response.send_message(
                "❌ Сбор уже начался.",
                ephemeral=True
            )
            return

        user_id = interaction.user.id

        if user_id not in gathering["participants"]:
            await interaction.response.send_message(
                "⚠️ Ты не участвуешь в этом сборе.",
                ephemeral=True
            )
            return

        gathering["participants"].remove(
            user_id
        )

        # Снова разрешаем объявление, если сбор впоследствии
        # будет набран заново.
        if len(gathering["participants"]) < gathering["max_players"]:
            gathering["full_announced"] = False

        await interaction.response.send_message(
            "🚪 Ты вышел из сбора.",
            ephemeral=True
        )

        await update_gathering_message(
            self.gathering_id
        )

    # ========================================================
    # НАЧАТЬ
    # ========================================================

    async def start_callback(
        self,
        interaction: discord.Interaction
    ):
        gathering = gatherings.get(
            self.gathering_id
        )

        if not gathering:
            await interaction.response.send_message(
                "❌ Этот сбор больше не существует.",
                ephemeral=True
            )
            return

        if gathering["started"]:
            await interaction.response.send_message(
                "❌ Сбор уже был начат.",
                ephemeral=True
            )
            return

        # ----------------------------------------------------
        # Проверяем количество
        # ----------------------------------------------------

        if len(gathering["participants"]) < gathering[
            "max_players"
        ]:
            await interaction.response.send_message(
                "❌ Пока недостаточно игроков.",
                ephemeral=True
            )
            return

        # ----------------------------------------------------
        # Можно ли начинать?
        # ----------------------------------------------------

        # Начать может:
        # 1. создатель
        # 2. администратор
        # 3. участник сбора

        is_creator = (
            interaction.user.id
            == gathering["creator_id"]
        )

        is_admin = (
            interaction.user.guild_permissions.administrator
        )

        is_participant = (
            interaction.user.id
            in gathering["participants"]
        )

        if not (
            is_creator
            or is_admin
            or is_participant
        ):
            await interaction.response.send_message(
                "❌ Начать сбор может только "
                "создатель, администратор или "
                "участник сбора.",
                ephemeral=True
            )
            return

        # ----------------------------------------------------
        # Получаем категорию
        # ----------------------------------------------------

        category = await get_voice_category(
            interaction.guild
        )

        if not category:
            await interaction.response.send_message(
                "❌ Я не могу создать голосовой канал.\n"
                "Проверь право **Manage Channels**.",
                ephemeral=True
            )
            return

        # ----------------------------------------------------
        # Создаём войс
        # ----------------------------------------------------

        voice_name = (
            f"🎮 {gathering['name']}"
        )

        try:
            voice_channel = await interaction.guild.create_voice_channel(
                name=voice_name[:100],
                category=category,
                reason=f"Запуск сбора №{self.gathering_id}"
            )

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ У бота нет права "
                "**Manage Channels**.",
                ephemeral=True
            )
            return

        except discord.HTTPException as error:
            await interaction.response.send_message(
                f"❌ Не удалось создать войс.\n"
                f"Ошибка: `{error}`",
                ephemeral=True
            )
            return

        # ----------------------------------------------------
        # Отмечаем сбор начатым
        # ----------------------------------------------------

        gathering["started"] = True
        gathering["voice_id"] = voice_channel.id

        # ----------------------------------------------------
        # Сначала отвечаем пользователю
        # ----------------------------------------------------

        await interaction.response.send_message(
            f"🚀 **Сбор начался!**\n"
            f"Голосовой канал: {voice_channel.mention}",
            ephemeral=True
        )

        # ----------------------------------------------------
        # Перемещаем участников
        # ----------------------------------------------------

        moved = 0
        not_in_voice = 0
        failed = 0

        for user_id in gathering["participants"]:
            member = interaction.guild.get_member(
                user_id
            )

            if not member:
                continue

            # Участник должен находиться в каком-либо войсе
            if not member.voice:
                not_in_voice += 1
                continue

            try:
                await member.move_to(
                    voice_channel,
                    reason=f"Перемещение в сбор №{self.gathering_id}"
                )

                moved += 1

            except discord.Forbidden:
                failed += 1

            except discord.HTTPException:
                failed += 1

        # ----------------------------------------------------
        # Обновляем сообщение сбора
        # ----------------------------------------------------

        await update_gathering_message(
            self.gathering_id
        )

        # ----------------------------------------------------
        # Отправляем информацию
        # ----------------------------------------------------

        text = (
            f"🚀 **Сбор запущен!**\n\n"
            f"🎮 **{gathering['name']}**\n"
            f"🔊 Войс: {voice_channel.mention}\n"
            f"👥 Участников: "
            f"{len(gathering['participants'])}\n\n"
            f"✅ Перемещено: {moved}\n"
            f"⚠️ Не были в войсе: {not_in_voice}\n"
            f"❌ Ошибок перемещения: {failed}"
        )

        try:
            await interaction.channel.send(
                text
            )
        except discord.Forbidden:
            pass



# ============================================================
# ДОПОЛНИТЕЛЬНЫЕ КОМАНДЫ
# ============================================================

DATA_FILE = Path("bot_data.json")

# Данные новых функций. Они сохраняются между перезапусками.
bot_data = {
    "subscribers": {},       # guild_id -> [user_id, ...]
    "notifications": {},    # guild_id:user_id -> bool
    "reminders": [],        # одноразовые напоминания
    "changelogs": {}        # guild_id -> [текст, ...]
}


def load_bot_data():
    global bot_data

    if not DATA_FILE.exists():
        return

    try:
        loaded = json.loads(DATA_FILE.read_text(encoding="utf-8"))

        if isinstance(loaded, dict):
            bot_data.update(loaded)

    except (json.JSONDecodeError, OSError):
        print("⚠️ Не удалось загрузить bot_data.json. Создаю данные заново.")


def save_bot_data():
    try:
        DATA_FILE.write_text(
            json.dumps(bot_data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except OSError as error:
        print(f"⚠️ Не удалось сохранить bot_data.json: {error}")


def parse_duration(value: str):
    """
    Поддерживает:
    30s = 30 секунд
    10m = 10 минут
    2h  = 2 часа
    1d  = 1 день
    """
    value = value.strip().lower()

    match = re.fullmatch(r"(\d+(?:\.\d+)?)(s|m|h|d)", value)

    if not match:
        return None

    amount = float(match.group(1))
    unit = match.group(2)

    multiplier = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400
    }[unit]

    seconds = amount * multiplier

    if seconds <= 0 or seconds > 365 * 86400:
        return None

    return seconds


async def send_text_to_channel(
    guild: discord.Guild,
    channel_id: int,
    content: str
):
    channel = guild.get_channel(channel_id)

    if not channel:
        return False

    try:
        await channel.send(content)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def reminder_worker(
    guild_id: int,
    channel_id: int,
    user_id: int,
    text: str,
    delay: float
):
    try:
        await asyncio.sleep(delay)

        guild = bot.get_guild(guild_id)

        if not guild:
            return

        await send_text_to_channel(
            guild,
            channel_id,
            f"⏰ <@{user_id}> **Напоминание:** {text}"
        )

    except asyncio.CancelledError:
        return


# ------------------------------------------------------------
# /serverinfo
# ------------------------------------------------------------

@bot.tree.command(
    name="serverinfo",
    description="Показать информацию о сервере"
)
@app_commands.guild_only()
async def serverinfo_command(interaction: discord.Interaction):
    guild = interaction.guild

    embed = discord.Embed(
        title=f"📊 Информация о сервере — {guild.name}",
        color=discord.Color.blurple()
    )

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(
        name="🆔 ID",
        value=str(guild.id),
        inline=True
    )

    embed.add_field(
        name="👥 Участники",
        value=str(guild.member_count),
        inline=True
    )

    embed.add_field(
        name="💬 Каналы",
        value=str(len(guild.channels)),
        inline=True
    )

    embed.add_field(
        name="🏷 Роли",
        value=str(len(guild.roles)),
        inline=True
    )

    embed.add_field(
        name="👑 Владелец",
        value=f"<@{guild.owner_id}>",
        inline=True
    )

    embed.add_field(
        name="📅 Создан",
        value=f"<t:{int(guild.created_at.timestamp())}:F>",
        inline=False
    )

    await interaction.response.send_message(embed=embed)


# ------------------------------------------------------------
# /verification
# ------------------------------------------------------------

@bot.tree.command(
    name="verification",
    description="Выдать себе роль верифицированного пользователя"
)
@app_commands.describe(
    role="Роль, которую бот должен выдать"
)
@app_commands.guild_only()
async def verification_command(
    interaction: discord.Interaction,
    role: discord.Role
):
    member = interaction.guild.get_member(interaction.user.id)

    if not member:
        await interaction.response.send_message(
            "❌ Не удалось найти тебя на сервере.",
            ephemeral=True
        )
        return

    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            "❌ Я не могу выдать эту роль. "
            "Моя самая высокая роль должна быть выше неё.",
            ephemeral=True
        )
        return

    if role in member.roles:
        await interaction.response.send_message(
            f"ℹ️ У тебя уже есть роль {role.mention}.",
            ephemeral=True
        )
        return

    try:
        await member.add_roles(
            role,
            reason="Верификация через /verification"
        )

        await interaction.response.send_message(
            f"✅ Верификация пройдена! Тебе выдана роль {role.mention}.",
            ephemeral=True
        )

    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ У бота нет права выдавать эту роль.",
            ephemeral=True
        )


# ------------------------------------------------------------
# /reminder
# ------------------------------------------------------------

@bot.tree.command(
    name="reminder",
    description="Создать напоминание"
)
@app_commands.describe(
    after="Через сколько: 30s, 10m, 2h, 1d",
    text="Текст напоминания"
)
@app_commands.guild_only()
async def reminder_command(
    interaction: discord.Interaction,
    after: str,
    text: str
):
    delay = parse_duration(after)

    if delay is None:
        await interaction.response.send_message(
            "❌ Неверный формат времени.\n"
            "Используй: `30s`, `10m`, `2h` или `1d`.",
            ephemeral=True
        )
        return

    reminder = {
        "guild_id": interaction.guild.id,
        "channel_id": interaction.channel.id,
        "user_id": interaction.user.id,
        "text": text,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "due_at": (
            datetime.now(timezone.utc)
            + timedelta(seconds=delay)
        ).isoformat()
    }

    bot_data["reminders"].append(reminder)
    save_bot_data()

    asyncio.create_task(
        reminder_worker(
            interaction.guild.id,
            interaction.channel.id,
            interaction.user.id,
            text,
            delay
        )
    )

    await interaction.response.send_message(
        f"⏰ Напоминание создано.\n"
        f"Сработает через **{after}**.",
        ephemeral=True
    )


# ------------------------------------------------------------
# /reminders
# ------------------------------------------------------------

@bot.tree.command(
    name="reminders",
    description="Показать мои напоминания"
)
@app_commands.guild_only()
async def reminders_command(interaction: discord.Interaction):
    now = datetime.now(timezone.utc)
    mine = []

    for item in bot_data["reminders"]:
        if item.get("guild_id") != interaction.guild.id:
            continue

        if item.get("user_id") != interaction.user.id:
            continue

        try:
            due = datetime.fromisoformat(item["due_at"])
        except (KeyError, ValueError):
            continue

        if due > now:
            mine.append((item, due))

    if not mine:
        await interaction.response.send_message(
            "📭 У тебя нет активных напоминаний.",
            ephemeral=True
        )
        return

    lines = []

    for index, (item, due) in enumerate(mine, start=1):
        lines.append(
            f"**{index}.** {item['text']} — <t:{int(due.timestamp())}:R>"
        )

    embed = discord.Embed(
        title="⏰ Мои напоминания",
        description="\n".join(lines),
        color=discord.Color.blurple()
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ------------------------------------------------------------
# /timer
# ------------------------------------------------------------

@bot.tree.command(
    name="timer",
    description="Запустить таймер"
)
@app_commands.describe(
    duration="Например: 30s, 10m, 2h",
    text="Что написать после окончания таймера"
)
@app_commands.guild_only()
async def timer_command(
    interaction: discord.Interaction,
    duration: str,
    text: str = "Время вышло!"
):
    delay = parse_duration(duration)

    if delay is None:
        await interaction.response.send_message(
            "❌ Неверный формат. Используй `30s`, `10m`, `2h` или `1d`.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"⏱️ Таймер на **{duration}** запущен."
    )

    async def timer_worker():
        await asyncio.sleep(delay)

        try:
            await interaction.channel.send(
                f"⏰ <@{interaction.user.id}> **{text}**"
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    asyncio.create_task(timer_worker())


# ------------------------------------------------------------
# /schedule
# ------------------------------------------------------------

@bot.tree.command(
    name="schedule",
    description="Запланировать сообщение на дату и время"
)
@app_commands.describe(
    when="Дата и время в формате YYYY-MM-DD HH:MM по UTC",
    text="Сообщение"
)
@app_commands.guild_only()
async def schedule_command(
    interaction: discord.Interaction,
    when: str,
    text: str
):
    try:
        target = datetime.strptime(
            when,
            "%Y-%m-%d %H:%M"
        ).replace(tzinfo=timezone.utc)

    except ValueError:
        await interaction.response.send_message(
            "❌ Формат должен быть: `YYYY-MM-DD HH:MM` (UTC).",
            ephemeral=True
        )
        return

    delay = (target - datetime.now(timezone.utc)).total_seconds()

    if delay <= 0:
        await interaction.response.send_message(
            "❌ Укажи время в будущем.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"📅 Сообщение запланировано на "
        f"<t:{int(target.timestamp())}:F>.",
        ephemeral=True
    )

    async def scheduled_worker():
        await asyncio.sleep(delay)

        try:
            await interaction.channel.send(text)
        except (discord.Forbidden, discord.HTTPException):
            pass

    asyncio.create_task(scheduled_worker())


# ------------------------------------------------------------
# /changelog
# ------------------------------------------------------------

@bot.tree.command(
    name="changelog",
    description="Показать или добавить изменение"
)
@app_commands.describe(
    text="Текст изменения. Если не указан — показать последние изменения."
)
@app_commands.guild_only()
async def changelog_command(
    interaction: discord.Interaction,
    text: Optional[str] = None
):
    guild_key = str(interaction.guild.id)

    if text:
        if not (
            interaction.user.guild_permissions.manage_guild
            or interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "❌ Добавлять изменения может только администрация.",
                ephemeral=True
            )
            return

        bot_data["changelogs"].setdefault(guild_key, [])
        bot_data["changelogs"][guild_key].append(
            f"<t:{int(datetime.now(timezone.utc).timestamp())}:d> — {text}"
        )

        bot_data["changelogs"][guild_key] = (
            bot_data["changelogs"][guild_key][-10:]
        )

        save_bot_data()

        await interaction.response.send_message(
            "✅ Изменение добавлено."
        )
        return

    changes = bot_data["changelogs"].get(guild_key, [])

    if not changes:
        await interaction.response.send_message(
            "📋 История изменений пока пустая."
        )
        return

    embed = discord.Embed(
        title="📋 Changelog",
        description="\n".join(reversed(changes)),
        color=discord.Color.blurple()
    )

    await interaction.response.send_message(embed=embed)


# ------------------------------------------------------------
# /notify
# ------------------------------------------------------------

@bot.tree.command(
    name="notify",
    description="Включить или выключить уведомления"
)
@app_commands.describe(
    enabled="true — включить, false — выключить"
)
@app_commands.guild_only()
async def notify_command(
    interaction: discord.Interaction,
    enabled: bool
):
    key = f"{interaction.guild.id}:{interaction.user.id}"

    bot_data["notifications"][key] = enabled
    save_bot_data()

    status = "включены 🔔" if enabled else "выключены 🔕"

    await interaction.response.send_message(
        f"✅ Уведомления для тебя {status}.",
        ephemeral=True
    )


# ------------------------------------------------------------
# /subscribe
# ------------------------------------------------------------

@bot.tree.command(
    name="subscribe",
    description="Подписаться на новости сервера"
)
@app_commands.guild_only()
async def subscribe_command(interaction: discord.Interaction):
    guild_key = str(interaction.guild.id)

    subscribers = bot_data["subscribers"].setdefault(
        guild_key,
        []
    )

    if interaction.user.id not in subscribers:
        subscribers.append(interaction.user.id)
        save_bot_data()

    await interaction.response.send_message(
        "🔔 Ты подписался на новости сервера.",
        ephemeral=True
    )


# ------------------------------------------------------------
# /unsubscribe
# ------------------------------------------------------------

@bot.tree.command(
    name="unsubscribe",
    description="Отписаться от новостей сервера"
)
@app_commands.guild_only()
async def unsubscribe_command(interaction: discord.Interaction):
    guild_key = str(interaction.guild.id)

    subscribers = bot_data["subscribers"].setdefault(
        guild_key,
        []
    )

    if interaction.user.id in subscribers:
        subscribers.remove(interaction.user.id)
        save_bot_data()

    save_bot_data()

    await interaction.response.send_message(
        "🔕 Ты отписался от новостей сервера.",
        ephemeral=True
    )


# ------------------------------------------------------------
# /news
# ------------------------------------------------------------

@bot.tree.command(
    name="news",
    description="Опубликовать новость сервера"
)
@app_commands.describe(
    text="Текст новости"
)
@app_commands.guild_only()
async def news_command(
    interaction: discord.Interaction,
    text: str
):
    if not (
        interaction.user.guild_permissions.manage_guild
        or interaction.user.guild_permissions.administrator
    ):
        await interaction.response.send_message(
            "❌ Публиковать новости может только администрация.",
            ephemeral=True
        )
        return

    guild_key = str(interaction.guild.id)

    subscribers = bot_data["subscribers"].get(
        guild_key,
        []
    )

    mentions = []

    for user_id in subscribers:
        key = f"{interaction.guild.id}:{user_id}"

        # Если notify выключен — не упоминаем пользователя.
        if bot_data["notifications"].get(key, True):
            mentions.append(f"<@{user_id}>")

    embed = discord.Embed(
        title="📰 Новость сервера",
        description=text,
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )

    content = ""

    if mentions:
        content = " ".join(mentions)

    await interaction.response.send_message(
        content=content,
        embed=embed,
        allowed_mentions=discord.AllowedMentions(
            users=True
        )
    )


# ============================================================
# ЗАГРУЗКА ДАННЫХ
# ============================================================

load_bot_data()


# ============================================================
# SLASH COMMAND /СБОР
# ============================================================

@bot.tree.command(
    name="сбор",
    description="Создать новый сбор игроков"
)
@app_commands.guild_only()
async def gathering_command(
    interaction: discord.Interaction
):
    # --------------------------------------------------------
    # Проверка прав
    # --------------------------------------------------------

    if not (
        interaction.user.guild_permissions.manage_guild
        or interaction.user.guild_permissions.administrator
    ):
        await interaction.response.send_message(
            "❌ У тебя нет права создавать сборы.\n"
            "Нужны права **Manage Server** или "
            "**Administrator**.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🎮 Создание сбора",
        description=(
            "Нажми кнопку ниже, чтобы создать новый сбор.\n\n"
            "Тебе потребуется указать:\n"
            "• название события\n"
            "• количество игроков\n"
            "• роль участников\n"
            "• время начала"
        ),
        color=discord.Color.blurple()
    )

    view = CreateGatheringView()

    await interaction.response.send_message(
        embed=embed,
        view=view,
        ephemeral=True
    )


# ============================================================
# КОМАНДА ДЛЯ УДАЛЕНИЯ СБОРА
# ============================================================

@bot.tree.command(
    name="удалить_сбор",
    description="Удалить сбор"
)
@app_commands.describe(
    gathering_id="Номер сбора"
)
@app_commands.guild_only()
async def delete_gathering(
    interaction: discord.Interaction,
    gathering_id: int
):
    if not (
        interaction.user.guild_permissions.manage_guild
        or interaction.user.guild_permissions.administrator
    ):
        await interaction.response.send_message(
            "❌ У тебя нет прав.",
            ephemeral=True
        )
        return

    gathering = gatherings.get(
        gathering_id
    )

    if not gathering:
        await interaction.response.send_message(
            "❌ Сбор с таким ID не найден.",
            ephemeral=True
        )
        return

    # Удаляем голосовой канал, если он есть
    if gathering["voice_id"]:
        voice = interaction.guild.get_channel(
            gathering["voice_id"]
        )

        if voice:
            try:
                await voice.delete(
                    reason="Удаление сбора"
                )
            except discord.HTTPException:
                pass

    del gatherings[gathering_id]

    await interaction.response.send_message(
        f"✅ Сбор №{gathering_id} удалён."
    )


# ============================================================
# СОБЫТИЕ READY
# ============================================================

@bot.event
async def on_ready():
    print(
        "========================================"
    )

    print(
        f"Бот запущен: {bot.user}"
    )

    print(
        f"ID бота: {bot.user.id}"
    )

    print(
        f"Серверов: {len(bot.guilds)}"
    )

    print(
        "========================================"
    )

    # --------------------------------------------------------
    # Синхронизация slash-команд
    # --------------------------------------------------------

    try:
        # Глобальная синхронизация
        synced = await bot.tree.sync()

        print(
            f"Глобально синхронизировано команд: {len(synced)}"
        )

        # Дополнительно синхронизируем команды
        # непосредственно с каждым сервером.
        # Благодаря этому новые команды появляются сразу.
        for guild in bot.guilds:
            try:
                bot.tree.copy_global_to(guild=guild)

                guild_synced = await bot.tree.sync(
                    guild=guild
                )

                print(
                    f"Сервер: {guild.name} | "
                    f"команд: {len(guild_synced)}"
                )

            except Exception as guild_error:
                print(
                    f"Ошибка синхронизации "
                    f"сервера {guild.name}: {guild_error}"
                )

    except Exception as error:
        print(
            f"Ошибка глобальной синхронизации: {error}"
        )

# ============================================================
# ЗАПУСК
# ============================================================

if not TOKEN:
    print("❌ ОШИБКА: переменная окружения DISCORD_TOKEN не задана!")
else:
    bot.run(TOKEN)
