# vending.py — 自分専用 PayPay 自販機（Discord bot 拡張 / discord.py Cog）
#
# 使い方:
#   1) このファイルを mein_v2.py と同じフォルダに置く
#   2) bot の起動処理に次を追加する:
#          await bot.load_extension("vending")
#      （同期済みコマンドは bot.tree.sync() で反映）
#   3) 自動受け取りを有効にする場合のみ、第3段（モバイルログイン）で取得した
#      トークンを paypay_mobile_token.json に {"access_token": "..."} の形で保存し、
#      vending_config.json に {"auto_receive": true} を書く。
#      それまでは「手動確認モード」: 買い手のリンクを検証し、オーナーに通知 →
#      オーナーがアプリで受け取り → ボタンを押すと商品が自動配送される。
#
# 状態はすべて JSON で管理する（このファイルと同じフォルダ）:
#   vending_products.json : 商品と在庫
#   vending_sales.json    : 売上ログ
#   vending_state.json    : 処理済みリンク（多重受け取り防止）
#
# 安全のための既定値:
#   - 自動受け取りは auto_receive=true のときだけ動く。それ以外は受け取りを実行しない。
#   - 金額が商品価格と一致しないリンクは処理しない。
#   - 同じリンクは二度処理しない（ハッシュを記録）。
#   - 商品は受け取り確認が取れてから初めて配送される。

import asyncio
import datetime
import hashlib
import json
import os

import discord
from discord import app_commands
from discord.ext import commands

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_FILE = os.path.join(BASE_DIR, "vending_products.json")
SALES_FILE = os.path.join(BASE_DIR, "vending_sales.json")
STATE_FILE = os.path.join(BASE_DIR, "vending_state.json")
CONFIG_FILE = os.path.join(BASE_DIR, "vending_config.json")
TOKEN_FILE = os.path.join(BASE_DIR, "paypay_mobile_token.json")

# オーナー（出品者）の Discord ユーザーID
OWNER_ID = 1222377998494470155

ORDER_TIMEOUT_SEC = 600  # 購入セッションの有効期限（10分）


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _now_iso():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).isoformat()


# ---------------------------------------------------------------- PayPay受け取り
class PayPayReceiver:
    """PayPaython_mobile を使ったリンク検証・受け取り。

    トークンはこのマシン上の paypay_mobile_token.json から読む。
    値はログ・チャットに出さない。
    """

    def __init__(self):
        self._client = None

    def available(self):
        return os.path.exists(TOKEN_FILE)

    def _get_client(self):
        if self._client is not None:
            return self._client
        # PayPaython_mobile 4.3.0 前提（コンストラクタに App Store 取得はない）
        from PayPaython_mobile import PayPay

        data = _load_json(TOKEN_FILE, {})
        self._client = PayPay(access_token=data.get("access_token"))
        return self._client

    def check_link(self, url):
        """リンク情報 {amount, raw} を返す。失敗時は例外。"""
        client = self._get_client()
        info = client.link_check(url)
        # 4.3.0 は LinkInfo(NamedTuple) を返す: amount 属性が金額
        amount = getattr(info, "amount", None)
        if amount is None and isinstance(info, dict):
            payload = info.get("payload", {})
            amount = (
                payload.get("amount")
                or payload.get("totalAmount")
                or (payload.get("linkInfo") or {}).get("amount")
            )
        return {"amount": amount, "raw": info}

    def receive(self, url):
        """受け取りを実行。成功したら True、失敗は例外（メッセージに理由）。"""
        client = self._get_client()
        result = client.link_receive(url)
        header = result.get("header", {}) if isinstance(result, dict) else {}
        if header.get("resultCode") not in (None, "S0000"):
            raise RuntimeError(f"receive failed: {header}")
        return True


# ---------------------------------------------------------------- モーダル
class ProductAddModal(discord.ui.Modal, title="商品を追加"):
    name = discord.ui.TextInput(label="商品名", max_length=80)
    price = discord.ui.TextInput(label="価格（円・数字のみ）", max_length=10)
    stock = discord.ui.TextInput(
        label="在庫（1行に1つ）",
        style=discord.TextStyle.paragraph,
        placeholder="コードやURLなど、買い手に送る内容を1行に1つ",
        max_length=4000,
    )

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = int(str(self.price).strip().replace(",", "").replace("¥", ""))
            if price <= 0:
                raise ValueError
        except Exception:
            await interaction.response.send_message(
                "価格は正の数字で入れてください。", ephemeral=True
            )
            return
        items = [s.strip() for s in str(self.stock).splitlines() if s.strip()]
        if not items:
            await interaction.response.send_message(
                "在庫が空です。1行に1つ入れてください。", ephemeral=True
            )
            return
        pid = self.cog.add_product(str(self.name).strip(), price, items)
        await interaction.response.send_message(
            f"商品を追加しました：**{self.name}** ¥{price:,}（在庫 {len(items)}）ID: {pid}",
            ephemeral=True,
        )


class StockAddModal(discord.ui.Modal, title="在庫を追加"):
    stock = discord.ui.TextInput(
        label="追加する在庫（1行に1つ）",
        style=discord.TextStyle.paragraph,
        max_length=4000,
    )

    def __init__(self, cog, product_id):
        super().__init__()
        self.cog = cog
        self.product_id = product_id

    async def on_submit(self, interaction: discord.Interaction):
        items = [s.strip() for s in str(self.stock).splitlines() if s.strip()]
        if not items:
            await interaction.response.send_message("在庫が空です。", ephemeral=True)
            return
        if self.product_id not in self.cog._products()["items"]:
            await interaction.response.send_message(
                "その商品が見つかりません。商品名は入力欄の候補から選んでください。",
                ephemeral=True,
            )
            return
        n = self.cog.add_stock(self.product_id, items)
        await interaction.response.send_message(
            f"在庫を追加しました（現在 {n} 個）。", ephemeral=True
        )


# ---------------------------------------------------------------- Cog 本体
class Vending(commands.GroupCog, name="自販機"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.receiver = PayPayReceiver()
        self.pending = {}  # user_id -> {"product_id","price","expires"}

    async def cog_load(self):
        # 再起動後も既存パネルのボタンが効くよう、永続Viewを登録する
        try:
            self.bot.add_view(ShopView(self))
        except Exception:
            pass

    async def start_purchase(self, interaction, pid):
        """商品選択後の処理: セッションを作り、リンク送付の案内を返す。"""
        data = self._products()
        p = data["items"].get(pid)
        if not p or not p.get("active") or not p["stock"]:
            await interaction.response.edit_message(
                content="在庫がありません。", view=None
            )
            return
        self.pending[interaction.user.id] = {
            "product_id": pid,
            "price": p["price"],
            "expires": asyncio.get_event_loop().time() + ORDER_TIMEOUT_SEC,
        }
        text = (
            f"**{p['name']}** ¥{p['price']:,}\n"
            f"PayPayで ¥{p['price']:,} の受け取りリンクを作成し、**このbotへのDM** でリンクを送ってください（10分以内）。\n"
            f"確認が取れ次第、商品をDMでお送りします。"
        )
        await interaction.response.edit_message(content=text, view=None)
        try:
            await interaction.user.send(text)
        except Exception:
            pass

    # ---- 状態ヘルパー
    def _products(self):
        return _load_json(PRODUCTS_FILE, {"next_id": 1, "items": {}})

    def _save_products(self, data):
        _save_json(PRODUCTS_FILE, data)

    def add_product(self, name, price, stock_items):
        data = self._products()
        pid = f"p{data['next_id']}"
        data["next_id"] += 1
        data["items"][pid] = {
            "name": name,
            "price": price,
            "stock": stock_items,
            "active": True,
            "created_at": _now_iso(),
        }
        self._save_products(data)
        return pid

    def add_stock(self, product_id, items):
        data = self._products()
        data["items"][product_id]["stock"].extend(items)
        self._save_products(data)
        return len(data["items"][product_id]["stock"])

    def _is_owner(self, interaction):
        return interaction.user.id == OWNER_ID

    async def _product_autocomplete(self, interaction, current):
        data = self._products()
        return [
            app_commands.Choice(name=f"{p['name']}（在庫 {len(p['stock'])}）", value=pid)
            for pid, p in data["items"].items()
            if p.get("active") and current.lower() in p["name"].lower()
        ][:25]

    # ---- オーナー用コマンド
    @app_commands.command(name="商品追加", description="（オーナー）商品と在庫を登録する")
    async def add(self, interaction: discord.Interaction):
        if not self._is_owner(interaction):
            await interaction.response.send_message("オーナー専用です。", ephemeral=True)
            return
        await interaction.response.send_modal(ProductAddModal(self))

    @app_commands.command(name="在庫追加", description="（オーナー）既存商品に在庫を足す")
    @app_commands.autocomplete(product=_product_autocomplete)
    async def restock(self, interaction: discord.Interaction, product: str):
        if not self._is_owner(interaction):
            await interaction.response.send_message("オーナー専用です。", ephemeral=True)
            return
        if product not in self._products()["items"]:
            await interaction.response.send_message(
                "その商品が見つかりません。商品名は入力欄の候補から選んでください。",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(StockAddModal(self, product))

    @app_commands.command(name="商品削除", description="（オーナー）商品を販売停止にする")
    @app_commands.autocomplete(product=_product_autocomplete)
    async def remove(self, interaction: discord.Interaction, product: str):
        if not self._is_owner(interaction):
            await interaction.response.send_message("オーナー専用です。", ephemeral=True)
            return
        data = self._products()
        if product in data["items"]:
            data["items"][product]["active"] = False
            self._save_products(data)
            await interaction.response.send_message("販売停止にしました。", ephemeral=True)
        else:
            await interaction.response.send_message("商品が見つかりません。", ephemeral=True)

    @app_commands.command(name="売上", description="（オーナー）売上を確認する")
    async def sales(self, interaction: discord.Interaction):
        if not self._is_owner(interaction):
            await interaction.response.send_message("オーナー専用です。", ephemeral=True)
            return
        rows = _load_json(SALES_FILE, [])
        total = sum(r["price"] for r in rows)
        lines = [f"累計 {len(rows)} 件 / ¥{total:,}"]
        for r in rows[-10:][::-1]:
            lines.append(f"- {r['ts'][:10]} {r['name']} ¥{r['price']:,}（購入者 {r['buyer_id']}）")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ---- 買い手用
    @app_commands.command(name="開く", description="自販機のパネルを表示する")
    async def open_shop(self, interaction: discord.Interaction):
        data = self._products()
        items = [
            (pid, p) for pid, p in data["items"].items() if p.get("active")
        ]
        if not items:
            await interaction.response.send_message(
                "いま販売中の商品はありません。", ephemeral=True
            )
            return
        await interaction.response.defer()
        embed = _shop_embed(items)
        view = ShopView(self)
        await interaction.followup.send(embed=embed, view=view)

    # ---- 支払いリンクの受付（DM）
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is not None:
            return
        if "paypay.ne.jp" not in message.content:
            return
        order = self.pending.get(message.author.id)
        if not order:
            return
        if order["expires"] < asyncio.get_event_loop().time():
            self.pending.pop(message.author.id, None)
            await message.channel.send("購入セッションの期限が切れました。もう一度 /自販機 開く からどうぞ。")
            return
        if order.get("awaiting_owner"):
            await message.channel.send("オーナーの受け取り確認待ちです。しばらくお待ちください。")
            return
        url = next(
            (w for w in message.content.split() if "paypay.ne.jp" in w),
            message.content.strip(),
        )
        await self._process_payment(message, order, url)

    async def _process_payment(self, message, order, url):
        state = _load_json(STATE_FILE, {"processed_links": []})
        link_hash = hashlib.sha256(url.encode()).hexdigest()
        if link_hash in state["processed_links"]:
            await message.channel.send("このリンクは処理済みです。新しいリンクを送ってください。")
            return

        data = self._products()
        product = data["items"].get(order["product_id"])
        if not product or not product["stock"]:
            self.pending.pop(message.author.id, None)
            await message.channel.send("在庫がなくなってしまいました。リンクは受け取っていません。")
            return

        config = _load_json(CONFIG_FILE, {})
        auto = bool(config.get("auto_receive")) and self.receiver.available()

        # 金額の検証（可能な場合）
        if self.receiver.available():
            try:
                info = await asyncio.to_thread(self.receiver.check_link, url)
                if info["amount"] is not None and int(info["amount"]) != product["price"]:
                    await message.channel.send(
                        f"金額が合いません（必要 ¥{product['price']:,}）。リンクは受け取っていません。"
                    )
                    return
            except Exception:
                pass  # 検証できない場合は受け取り結果で判定する

        auto_error = None
        if auto:
            try:
                await asyncio.to_thread(self.receiver.receive, url)
                await self._deliver(message, order, product, link_hash, state)
                return
            except Exception as e:
                auto_error = e  # 手動確認モードへフォールバックする
        # 手動確認モード: オーナーに通知し、受け取りボタンを待つ
        self.pending[message.author.id] = {**order, "url": url, "awaiting_owner": True}
        owner = self.bot.get_user(OWNER_ID) or await self.bot.fetch_user(OWNER_ID)
        view = OwnerConfirmView(self, message.author.id)
        try:
            note = (
                f"\n⚠️ 自動受け取りに失敗したため手動確認に回しました：{auto_error}"
                if auto_error
                else ""
            )
            await owner.send(
                f"🧾 自販機：受け取り確認待ち\n"
                f"商品: {product['name']} ¥{product['price']:,}\n"
                f"購入者: {message.author} ({message.author.id})\n"
                f"リンク: {url}\n"
                f"アプリで受け取ったら下のボタンを押してください（押すと商品が自動配送されます）。"
                f"{note}",
                view=view,
            )
            await message.channel.send(
                "リンクを受け付けました。オーナーの受け取り確認後に商品が届きます。"
            )
        except Exception:
            await message.channel.send("オーナーへの通知に失敗しました。時間をおいて再度お試しください。")

    async def _deliver(self, message, order, product, link_hash, state):
        data = self._products()
        p = data["items"][order["product_id"]]
        if not p["stock"]:
            await message.channel.send("在庫がなくなってしまいました。オーナーに連絡してください。")
            return
        item = p["stock"].pop(0)
        self._save_products(data)
        state["processed_links"].append(link_hash)
        _save_json(STATE_FILE, state)
        sales = _load_json(SALES_FILE, [])
        sales.append(
            {
                "ts": _now_iso(),
                "buyer_id": message.author.id,
                "product_id": order["product_id"],
                "name": p["name"],
                "price": p["price"],
                "link_hash": link_hash,
            }
        )
        _save_json(SALES_FILE, sales)
        self.pending.pop(message.author.id, None)
        await message.channel.send(
            f"✅ 購入ありがとうございます！\n商品: **{p['name']}**\n```\n{item}\n```"
        )
        owner = self.bot.get_user(OWNER_ID) or await self.bot.fetch_user(OWNER_ID)
        try:
            await owner.send(
                f"💰 自販機で売れました：{p['name']} ¥{p['price']:,}（購入者 {message.author}）残り在庫 {len(p['stock'])}"
            )
        except Exception:
            pass


# ---------------------------------------------------------------- View 類
def _shop_embed(items):
    """自販機パネルのEmbed。

    商品ごとに「名前」の下に「`値段：¥○○`」（値段の行全体を四角く囲った表示）。
    在庫数は出さない（在庫確認ボタンで本人だけに見せる）。
    """
    embed = discord.Embed(title="🛒 自販機", color=0xE60012)
    for pid, p in items[:25]:
        value = f"`値段：¥{p['price']:,}`"
        if not p["stock"]:
            value += "\n（売り切れ）"
        embed.add_field(name=p["name"], value=value, inline=False)
    embed.set_footer(text="「購入」ボタンから買えます。在庫は「在庫確認」で見られます。")
    return embed


class BuySelectView(discord.ui.View):
    """購入ボタン押下後に本人だけに表示する、商品選択メニュー。"""

    def __init__(self, cog, items):
        super().__init__(timeout=300)
        self.cog = cog
        options = [
            discord.SelectOption(
                label=f"{p['name']} — ¥{p['price']:,}"[:100], value=pid
            )
            for pid, p in items[:25]
        ]
        select = discord.ui.Select(placeholder="買いたい商品を選ぶ", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        await self.cog.start_purchase(interaction, interaction.data["values"][0])


class ShopView(discord.ui.View):
    """「購入」「在庫確認」の2ボタンだけを持つ常設パネル用View。

    timeout=None の永続View。商品が増えてもボタンは増えない。
    購入: 押した本人にだけ商品選択メニューを出す。
    在庫確認: 押した本人にだけ在庫数を出す。
    bot再起動後は cog_load で再登録されるため、過去のパネルでも効く。
    """

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog
        buy = discord.ui.Button(
            label="購入",
            style=discord.ButtonStyle.primary,
            custom_id="vending_buy_open",
        )
        buy.callback = self._on_buy
        self.add_item(buy)
        check = discord.ui.Button(
            label="在庫確認",
            style=discord.ButtonStyle.secondary,
            custom_id="vending_stock_check",
        )
        check.callback = self._on_stock_check
        self.add_item(check)

    async def _on_buy(self, interaction: discord.Interaction):
        data = self.cog._products()
        items = [
            (pid, p)
            for pid, p in data["items"].items()
            if p.get("active") and p["stock"]
        ]
        if not items:
            await interaction.response.send_message(
                "いま在庫がある商品がありません。", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "買いたい商品を選んでください。",
            view=BuySelectView(self.cog, items),
            ephemeral=True,
        )

    async def _on_stock_check(self, interaction: discord.Interaction):
        data = self.cog._products()
        items = [
            (pid, p) for pid, p in data["items"].items() if p.get("active")
        ]
        if not items:
            await interaction.response.send_message(
                "いま販売中の商品はありません。", ephemeral=True
            )
            return
        embed = discord.Embed(title="📦 在庫状況", color=0xE60012)
        for pid, p in items[:25]:
            embed.add_field(
                name=p["name"],
                value=f"在庫：{len(p['stock'])}個" if p["stock"] else "在庫：売り切れ",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class OwnerConfirmView(discord.ui.View):
    def __init__(self, cog, buyer_id):
        super().__init__(timeout=3600)
        self.cog = cog
        self.buyer_id = buyer_id

    @discord.ui.button(label="受け取り完了（商品を配送）", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("オーナー専用です。", ephemeral=True)
            return
        order = self.cog.pending.get(self.buyer_id)
        if not order or not order.get("awaiting_owner"):
            await interaction.response.send_message("対象の注文が見つかりません。", ephemeral=True)
            return
        buyer = self.cog.bot.get_user(self.buyer_id) or await self.cog.bot.fetch_user(self.buyer_id)
        dm = buyer.dm_channel or await buyer.create_dm()
        state = _load_json(STATE_FILE, {"processed_links": []})
        link_hash = hashlib.sha256(order["url"].encode()).hexdigest()

        class _Msg:
            author = buyer
            channel = dm

        await self.cog._deliver(_Msg(), order, None, link_hash, state)
        await interaction.response.send_message("配送しました。", ephemeral=True)
        self.stop()

    @discord.ui.button(label="受け取れなかった（キャンセル）", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("オーナー専用です。", ephemeral=True)
            return
        self.cog.pending.pop(self.buyer_id, None)
        buyer = self.cog.bot.get_user(self.buyer_id) or await self.cog.bot.fetch_user(self.buyer_id)
        try:
            await buyer.send("受け取りが確認できなかったため、購入はキャンセルされました。")
        except Exception:
            pass
        await interaction.response.send_message("キャンセルしました。", ephemeral=True)
        self.stop()


async def setup(bot):
    await bot.add_cog(Vending(bot))
