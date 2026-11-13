import sys
import json
import html
import time
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from loguru import logger

from config import APP_VERSION, get_telegram_credentials
from db import init_db, seed_db, calculate_rice, cas_update_feature
from agent_po import (
    analyze_all_pending_feedbacks,
    ingest_single_feedback,
    prioritize_backlog,
    generate_user_story
)

logger.remove()
LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level} | [{extra[agent]}] {message}"
logger.configure(extra={"agent": "PO-BOT"})
logger.add(sys.stderr, format=LOG_FORMAT)
logger = logger.bind(agent="PO-BOT")

TOKEN, CHAT_ID = get_telegram_credentials()
if not TOKEN:
    logger.critical("Missing TELEGRAM_BOT_TOKEN in telegram_keys.py or .env")
    sys.exit(1)

bot = telebot.TeleBot(TOKEN)


def safe_delete_message(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def safe_edit_reply_markup(chat_id, message_id, reply_markup=None):
    try:
        bot.edit_message_reply_markup(chat_id, message_id, reply_markup=reply_markup)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Telegram markup edit warning: {e}")


def safe_edit_text(chat_id, message_id, text, reply_markup=None):
    try:
        bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=reply_markup)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Telegram text edit warning: {e}")


def build_feature_keyboard(feat_id, row_version):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("⚡ Effort -0.5w", callback_data=f"eff_{feat_id}_{-0.5}_{row_version}"),
        InlineKeyboardButton("🧱 Effort +0.5w", callback_data=f"eff_{feat_id}_{0.5}_{row_version}")
    )
    markup.row(
        InlineKeyboardButton("📝 Générer User Story (Bloc 3)", callback_data=f"us_{feat_id}_{row_version}")
    )
    markup.row(
        InlineKeyboardButton("⏩ Skip (Suivant)", callback_data=f"skip_{feat_id}"),
        InlineKeyboardButton("🗑️ Dismiss", callback_data=f"dismiss_{feat_id}")
    )
    return markup


def format_feature_card(conn, feat):
    verbatims = conn.execute(
        "SELECT channel, customer_name, segment, raw_text FROM feedbacks WHERE feature_id = ? LIMIT 3",
        (feat["id"],)
    ).fetchall()

    moscow_icons = {
        "Must Have": "🔴 <b>MUST HAVE</b>",
        "Should Have": "🟠 <b>SHOULD HAVE</b>",
        "Could Have": "🟡 <b>COULD HAVE</b>",
        "Won't Have": "⚪ <b>WON'T HAVE</b>"
    }
    badge = moscow_icons.get(feat["moscow"], "🔵 <b>PRIORITIZED</b>")

    lines = [
        f"{badge} | 🏆 <b>RICE : {feat['rice_score']}</b>",
        f"📌 <b>{html.escape(feat['title'])}</b> (v{feat['row_version']})\n",
        f"🎯 <b>Problème :</b>\n<i>{html.escape(feat['problem_statement'] or '')}</i>\n",
        f"📐 <b>Détail du calcul RICE :</b>",
        f"• Reach (Portée pondérée) : <b>{feat['reach']}</b> ({feat['evidence_count']} retours)",
        f"• Impact : <b>{feat['impact']}</b> | Confiance : <b>{int(feat['confidence']*100)}%</b>",
        f"• Effort estimé : <b>{feat['effort']} sem-pers.</b>\n",
        f"🧠 <b>Justification IA :</b>\n{html.escape(feat['rationale'] or 'En attente de scoring (/prioritize)')}\n"
    ]

    if verbatims:
        lines.append("💬 <b>Preuves clients rattachées :</b>")
        for v in verbatims:
            snippet = html.escape(v["raw_text"][:110] + ("..." if len(v["raw_text"]) > 110 else ""))
            lines.append(f"• <b>[{html.escape(v['segment'])}] {html.escape(v['customer_name'])}</b> : <i>\"{snippet}\"</i>")

    return "\n".join(lines)


def send_next_feature(chat_id, specific_id=None):
    conn = init_db()
    if specific_id:
        feat = conn.execute("SELECT * FROM features WHERE id = ?", (specific_id,)).fetchone()
    else:
        feat = conn.execute("SELECT * FROM features ORDER BY rice_score DESC, updated_at ASC LIMIT 1").fetchone()

    if not feat:
        bot.send_message(chat_id, "📭 Aucun élément dans le backlog. Lancez /seed pour charger les données.")
        conn.close()
        return

    text = format_feature_card(conn, feat)
    markup = build_feature_keyboard(feat["id"], feat["row_version"])
    conn.close()
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    text = (
        "🤖 <b>FLOWPULSE PO COPILOT - THIGA CASE STUDY</b>\n\n"
        "Assistant IA d'aide à la décision pour Product Owner :\n\n"
        "<b>1. COMPRENDRE (Analyse des feedbacks)</b>\n"
        "• /seed - Réinitialiser le jeu de données démo (12 feedbacks + 3 features)\n"
        "• /ingest - Analyser, filtrer le bruit SAV et regrouper les feedbacks\n"
        "• <i>Collez un texte libre dans le chat</i> - Ingestion temps réel d'un feedback client\n\n"
        "<b>2. ARBITRER (Aide à la priorisation)</b>\n"
        "• /prioritize - Calculer les scores RICE et MoSCoW avec justifications\n"
        "• /backlog - Parcourir et ajuster les fonctionnalités priorisées\n\n"
        "<b>3. FORMALISER (Rédaction assistée)</b>\n"
        "• Cliquez sur <i>Générer User Story</i> depuis une carte du backlog pour produire la User Story, les critères Gherkin et le chiffrage Fibonacci."
    )
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🔄 1. Charger Démo (/seed)", callback_data="cmd_seed"),
        InlineKeyboardButton("📥 2. Analyser (/ingest)", callback_data="cmd_ingest")
    )
    markup.row(
        InlineKeyboardButton("⚖️ 3. Prioriser (/prioritize)", callback_data="cmd_prio"),
        InlineKeyboardButton("📊 4. Voir Backlog", callback_data="cmd_backlog")
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)


@bot.message_handler(commands=["seed"])
def handle_seed(message):
    conn = seed_db(reset=True)
    conn.close()
    logger.info("🌱 Demo database reset and seeded.")
    bot.send_message(
        message.chat.id,
        "✅ <b>Base initialisée !</b>\n• 3 fonctionnalités existantes au backlog\n• 12 feedbacks multicanaux bruts (Emails, Zendesk, Slack, In-App) prêts à être analysés.\n\n👉 Lancez /ingest pour démarrer le Bloc 1.",
        parse_mode="HTML"
    )


@bot.message_handler(commands=["ingest"])
def handle_ingest(message):
    conn = init_db()
    pending_cnt = conn.execute("SELECT COUNT(*) FROM feedbacks WHERE processed = 0").fetchone()[0]
    if pending_cnt == 0:
        bot.send_message(message.chat.id, "ℹ️ Tous les feedbacks ont déjà été analysés. Collez un nouveau message client ou lancez /prioritize.")
        conn.close()
        return

    wait_msg = bot.send_message(message.chat.id, f"🧠 <b>Bloc 1 [COMPRENDRE] :</b> Analyse et clustering de {pending_cnt} feedbacks multicanaux en cours...", parse_mode="HTML")
    stats = analyze_all_pending_feedbacks(conn)

    features = conn.execute("SELECT title, evidence_count, reach, status FROM features ORDER BY evidence_count DESC").fetchall()
    noise_rows = conn.execute("SELECT customer_name, channel, extracted_need FROM feedbacks WHERE category = 'SUPPORT_NOISE'").fetchall()
    conn.close()

    lines = [
        "✅ <b>BLOC 1 TERMINÉ : ANALYSE DES FEEDBACKS</b>\n",
        f"• Feedbacks traités : <b>{stats['processed']}</b>",
        f"• Besoins produits rattachés : <b>{stats['linked']}</b>",
        f"• Bruit SAV écarté du backlog : <b>{stats['noise']}</b>\n",
        "📈 <b>Tendances & Motifs identifiés :</b>"
    ]
    for f in features:
        tag = "🆕 Émergent" if f["status"] == "candidate" else "📌 Existant"
        lines.append(f"• {tag} : <b>{html.escape(f['title'])}</b> ({f['evidence_count']} preuves | Reach pondéré: {f['reach']})")

    if noise_rows:
        lines.append("\n🧹 <b>Bruit hors-produit filtré (SAV/Compta) :</b>")
        for n in noise_rows:
            lines.append(f"• <i>{html.escape(n['customer_name'])} ({html.escape(n['channel'])})</i> : {html.escape(n['extracted_need'])}")

    lines.append("\n👉 Passez au Bloc 2 avec /prioritize")
    safe_delete_message(message.chat.id, wait_msg.message_id)
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")


@bot.message_handler(commands=["prioritize"])
def handle_prioritize(message):
    conn = init_db()
    wait_msg = bot.send_message(message.chat.id, "⚖️ <b>Bloc 2 [ARBITRER] :</b> Calcul des scores RICE, classification MoSCoW et justification des arbitrages...", parse_mode="HTML")
    count = prioritize_backlog(conn)
    rows = conn.execute("SELECT id, title, rice_score, moscow, reach, impact, effort FROM features ORDER BY rice_score DESC").fetchall()
    conn.close()

    lines = [f"🏆 <b>MATRICE DE PRIORISATION ({count} fonctionnalités évaluées)</b>\n"]
    for idx, r in enumerate(rows, 1):
        lines.append(
            f"<b>#{idx}. {html.escape(r['title'])}</b>\n"
            f"   └ <b>RICE : {r['rice_score']}</b> | <code>{r['moscow']}</code> (Reach:{r['reach']} | Imp:{r['impact']} | Eff:{r['effort']}w)"
        )
    lines.append("\n👇 <b>Inspection détaillée de la priorité #1 :</b>")
    safe_delete_message(message.chat.id, wait_msg.message_id)
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")
    send_next_feature(message.chat.id, specific_id=rows[0]["id"] if rows else None)


@bot.message_handler(commands=["backlog"])
def handle_backlog(message):
    send_next_feature(message.chat.id)


@bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))
def handle_live_feedback_paste(message):
    """Live demo feature: paste any raw customer message in Telegram to triage and update RICE on the fly."""
    conn = init_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO feedbacks (channel, customer_name, segment, raw_text, processed)
        VALUES ('Direct Telegram', 'Client Live Demo', 'Enterprise', ?, 0)
    """, (message.text.strip(),))
    conn.commit()
    fb_id = cur.lastrowid
    fb_row = cur.execute("SELECT * FROM feedbacks WHERE id = ?", (fb_id,)).fetchone()

    res = ingest_single_feedback(conn, fb_row)
    updated_fb = cur.execute("SELECT feature_id FROM feedbacks WHERE id = ?", (fb_id,)).fetchone()
    feat = None
    if updated_fb and updated_fb["feature_id"]:
        feat = cur.execute("SELECT * FROM features WHERE id = ?", (updated_fb["feature_id"],)).fetchone()
    conn.close()

    if res["category"] == "SUPPORT_NOISE":
        bot.send_message(
            message.chat.id,
            f"🧹 <b>Feedback classé en BRUIT SAV (Support/Admin)</b>\n"
            f"• Synthèse : <i>{html.escape(res['extracted_need'])}</i>\n"
            f"• Action : Écarté du backlog produit.",
            parse_mode="HTML"
        )
    elif feat:
        bot.send_message(
            message.chat.id,
            f"📥 <b>Feedback rattaché en temps réel !</b>\n"
            f"• Catégorie : <code>{res['category']}</code> | Sentiment : <code>{res['sentiment']}</code>\n"
            f"• Besoin extrait : <i>{html.escape(res['extracted_need'])}</i>\n"
            f"• Fonctionnalité impactée : <b>{html.escape(feat['title'])}</b>\n"
            f"• Nouveau Reach : <b>{feat['reach']}</b> -> <b>Nouveau RICE : {feat['rice_score']}</b>",
            parse_mode="HTML"
        )
        send_next_feature(message.chat.id, specific_id=feat["id"])


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    data = call.data
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if data == "cmd_seed":
        handle_seed(call.message)
        return
    elif data == "cmd_ingest":
        handle_ingest(call.message)
        return
    elif data == "cmd_prio":
        handle_prioritize(call.message)
        return
    elif data == "cmd_backlog":
        send_next_feature(chat_id)
        return

    parts = data.split("_")
    action = parts[0]
    feat_id = int(parts[1])

    conn = init_db()
    cur = conn.cursor()

    if action == "dismiss":
        safe_delete_message(chat_id, msg_id)

    elif action == "skip":
        # Functional rule: Skip = visual dismiss + rotate updated_at + show next feature without status mutation
        cur.execute("UPDATE features SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (feat_id,))
        conn.commit()
        safe_delete_message(chat_id, msg_id)
        next_feat = cur.execute("SELECT id FROM features WHERE id != ? ORDER BY updated_at ASC, rice_score DESC LIMIT 1", (feat_id,)).fetchone()
        conn.close()
        send_next_feature(chat_id, specific_id=next_feat["id"] if next_feat else feat_id)
        return

    elif action == "eff":
        delta = float(parts[2])
        expected_v = int(parts[3])
        feat = cur.execute("SELECT * FROM features WHERE id = ?", (feat_id,)).fetchone()
        if feat:
            new_effort = max(0.5, round(feat["effort"] + delta, 1))
            new_rice = calculate_rice(feat["reach"], feat["impact"], feat["confidence"], new_effort)
            ok = cas_update_feature(
                conn, feat_id, expected_v,
                "effort = ?, rice_score = ?",
                (new_effort, new_rice)
            )
            updated = cur.execute("SELECT * FROM features WHERE id = ?", (feat_id,)).fetchone()
            if ok and updated:
                new_text = format_feature_card(conn, updated)
                new_markup = build_feature_keyboard(feat_id, updated["row_version"])
                safe_edit_text(chat_id, msg_id, new_text, reply_markup=new_markup)

    elif action == "us":
        wait_msg = bot.send_message(chat_id, "✍️ <b>Bloc 3 [FORMALISER] :</b> Rédaction de la User Story, des scénarios Gherkin et estimation Fibonacci...", parse_mode="HTML")
        us, probs = generate_user_story(conn, feat_id)
        feat = cur.execute("SELECT title, rice_score, moscow FROM features WHERE id = ?", (feat_id,)).fetchone()
        safe_delete_message(chat_id, wait_msg.message_id)

        ac_lines = "\n".join([f"• {html.escape(ac)}" for ac in us["acceptance_criteria"]])
        card = (
            f"📝 <b>USER STORY FORMALISÉE (BLOC 3)</b>\n"
            f"📌 <b>Feature :</b> {html.escape(feat['title'])}\n"
            f"🏷️ <code>{feat['moscow']}</code> | 🏆 RICE : <b>{feat['rice_score']}</b>\n\n"
            f"👤 <b>User Story :</b>\n<i>\"{html.escape(us['story_text'])}\"</i>\n\n"
            f"✅ <b>Critères d'Acceptation (Gherkin) :</b>\n{ac_lines}\n\n"
            f"📐 <b>Complexité relative :</b> <b>{us['story_points']} pts Fibonacci</b> (Taille <b>{html.escape(us.get('tshirt_size', 'M'))}</b>)\n"
            f"✂️ <b>Arbitrage & Découpage MVP :</b>\n<i>{html.escape(us.get('mvp_slice_suggestion', ''))}</i>"
        )
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("✅ Valider (Ready for Dev)", callback_data=f"appus_{feat_id}"),
            InlineKeyboardButton("🔄 Régénérer", callback_data=f"us_{feat_id}_0")
        )
        markup.row(InlineKeyboardButton("🗑️ Dismiss", callback_data=f"dismiss_{feat_id}"))
        bot.send_message(chat_id, card, parse_mode="HTML", reply_markup=markup)

    elif action == "appus":
        cur.execute("UPDATE user_stories SET status = 'approved' WHERE feature_id = ?", (feat_id,))
        cur.execute("UPDATE features SET status = 'approved' WHERE id = ?", (feat_id,))
        conn.commit()
        safe_edit_reply_markup(chat_id, msg_id, reply_markup=None)
        bot.send_message(chat_id, f"🎉 <b>User Story #{feat_id} validée (Ready for Dev) !</b>", parse_mode="HTML")

    conn.close()


if __name__ == "__main__":
    init_db()
    logger.info(f"🚀 Launching FlowPulse PO Copilot Bot V{APP_VERSION}...")
    try:
        bot.set_my_commands([
            BotCommand("seed", "Réinitialiser le jeu de données démo"),
            BotCommand("ingest", "Bloc 1 : Analyser et grouper les feedbacks"),
            BotCommand("prioritize", "Bloc 2 : Scorer le backlog (RICE & MoSCoW)"),
            BotCommand("backlog", "Inspecter et simuler les priorités du backlog"),
            BotCommand("help", "Afficher l'aide et l'architecture")
        ])
    except Exception as e:
        logger.warning(f"Could not set bot commands: {e}")

    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            logger.error(f"Polling error: {e}. Retrying in 3s...")
            time.sleep(3)