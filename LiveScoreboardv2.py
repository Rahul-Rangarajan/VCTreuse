import json
import os
import traceback
import discord
import requests
from discord.ext import commands, tasks
import asyncio
import re
from datetime import datetime
from apiCalls import get_live_matches, get_upcoming, get_health

TOKEN = os.getenv("TOKEN")
CHANNEL_ID = int(os.getenv("CHANNELID"))

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

update_allowed = asyncio.Event()
update_allowed.set()  # Initially allow updates

pause_allowed = asyncio.Event()
pause_allowed.set()

match_id_to_message = {}
match_id_to_embed = {}
match_history = {}
seen_ids = set()

last_known_scores = {}  # Tracks score for map count logic

finalized_matches = set()

TBD = ['tbd', 'unknown', None]


def parse_time_to_seconds(time_str):
    # Extract numbers and units using regex
    matches = re.findall(r'(\d+)\s*([dhms])', time_str)

    total_seconds = 0
    for value, unit in matches:
        value = int(value)
        if unit == 'd':
            total_seconds += value * 86400  # 24 * 60 * 60
        elif unit == 'h':
            total_seconds += value * 3600
        elif unit == 'm':
            total_seconds += value * 60
        elif unit == 's':
            total_seconds += value

    return total_seconds > 900


def format_score(t_score, ct_score):
    def safe_int(value, label):
        try:
            return int(value)
        except (TypeError, ValueError):
            print(f"[Warning] Invalid score for {label}: {value}")
            return 0

    t = safe_int(t_score, "T-side")
    ct = safe_int(ct_score, "CT-side")
    return str(t + ct)


def fix_round_totals(round_t1, round_t2):
    format_score(round_t1, 0)
    format_score(round_t2, 0)
    if round_t1 == 0 and round_t2 == 0:
        return round_t1, round_t2
    while max(int(round_t1), int(round_t2)) < 13 or abs(int(round_t1) - int(round_t2)) < 2:
        if int(round_t1) - int(round_t2) > 0:
            round_t1 = str(int(round_t1) + 1)
        elif int(round_t1) - int(round_t2) < 0:
            round_t2 = str(int(round_t2) + 1)

    return round_t1, round_t2


def log_payload_on_exception(payload: dict, context: str = "unknown"):
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"logs/ErrorPayload/error_payload_{context}_{timestamp}.json"
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4)
        print(f"[Error] Payload logged to {filename}")
    except Exception as e:
        print(f"[Logging Error] Failed to write payload: {e}")


def is_api_healthy():
    try:
        response = get_health()
        response.raise_for_status()
        health_data = response.json()

        vlrggapi_status = health_data.get("https://vlrggapi.vercel.app", {}).get("status")
        vlrgg_status = health_data.get("https://vlr.gg", {}).get("status")

        if vlrggapi_status == "Healthy" and vlrgg_status == "Healthy":
            print(f"[HEALTH] All Good")
            return True
        else:
            print(f"[HEALTH] One or more APIs unhealthy: vlrggapi={vlrggapi_status}, vlrgg={vlrgg_status}")
            return False

    except Exception as e:
        print(f"[HEALTH CHECK ERROR] {e}")
        return False

def build_match_description(match_id, match_data, match_history, status="LIVE"):
    team1 = match_data["team1"]
    team2 = match_data["team2"]
    score1 = match_data["score1"]
    score2 = match_data["score2"]

    map_number = 1 if match_data["map_number"] == "Unknown" else int(match_data["map_number"])
    print(map_number)
    map_name = match_data["current_map"]
    round1 = format_score(match_data["team1_round_t"], match_data["team1_round_ct"])
    round2 = format_score(match_data["team2_round_t"], match_data["team2_round_ct"])

    if match_id not in match_history:
        match_history[match_id] = {}

    # Detect if map_number reset backward (e.g. from 3 to 1)
    existing_map_numbers = match_history[match_id].keys()
    if existing_map_numbers:
        max_map = max(existing_map_numbers)

        if map_number < max_map:
            if map_number in match_history[match_id]:
                _, r1, r2 = match_history[match_id][map_number]
                if int(r1) > 0 or int(r2) > 0:
                    print(
                        f"[Map Rewind] Skipping update for match_id={match_id}, map_number={map_number}. Existing r1={r1}, r2={r2}")
                    return None

    prev_score1, prev_score2 = last_known_scores.get(match_id, (int(score1), int(score2)))
    if (int(score1) != int(prev_score1) or int(score2) != int(prev_score2)) and (int(round1) == 0 and int(round2) == 0):
        temp_map_name, r1, r2 = match_history[match_id][map_number]
        r1, r2 = fix_round_totals(r1, r2)
        match_history[match_id][map_number] = (temp_map_name, r1, r2)

    found_existing = False
    for key, (existing_map_name, r1, r2) in match_history[match_id].items():
        if existing_map_name == map_name:
            map_number = key
            found_existing = True
            break
            # Handle TBD → real map name transition
        elif existing_map_name.strip().lower() in TBD and map_name.strip().lower() not in TBD:
            match_history[match_id][key] = (map_name, r1, r2)
            map_number = key
            found_existing = True
            print(f"[Map Update] Updated TBD to actual map name for match {match_id}, map {key}: {map_name}")
            break
    if not found_existing:
        while map_number in match_history[match_id]:
            map_number += 1

    previous_entry = match_history[match_id].get(map_number)
    if previous_entry:
        _, prev_round1, prev_round2 = previous_entry
        if int(round1) > int(prev_round1):
            print("Prev1: ", prev_round1)
            print("Curr1: ", round1)
            prev_round1 = round1
        if int(round2) > int(prev_round2):
            print("Prev2: ", prev_round2)
            print("Curr2: ", round2)
            prev_round2 = round2

        match_history[match_id][map_number] = (map_name, prev_round1, prev_round2)
    else:
        match_history[match_id][map_number] = (map_name, round1, round2)

    last_known_scores[match_id] = (int(score1), int(score2))

    header = "🎮 Match Results" if status == "FINAL" else "🎮 Current Score (LIVE)"
    desc = f"{header}\n{team1} {score1} - {score2} {team2}\n"

    for i in sorted(match_history[match_id].keys()):
        map_n, r1, r2 = match_history[match_id][i]
        desc += f"\n🗺️️ Map: {map_n}\n{team1}: {r1}\n{team2}: {r2}"

    return desc


@tasks.loop(seconds=60)
async def update_matches():
    await update_allowed.wait()
    if not is_api_healthy():
        print("[SKIP] API unhealthy, skipping match update")
        return
    if len(seen_ids) < 1:
        pause_allowed.set()
    try:
        match_id = "Unknown"  # default value
        response = None  # default value

        seen_ids.clear()

        response = get_live_matches()
        response.raise_for_status()
        data = response.json()
        data = data['data']['segments']

        # data = response['data']['segments']

        for match in data:
            if "VCT" in match["match_event"]:
                pause_allowed.clear()

                match_id = match['match_page']
                seen_ids.add(match_id)

                description = build_match_description(match_id, match, match_history)
                if description is None:
                    print(
                        f"[SKIPPED] build_match_description returned None for {match_id} ({match['team1']} vs {match['team2']})")
                    continue  # Skip this match update

                title = f"{match['team1']} vs {match['team2']}"
                url = match['match_page']
                event = f"{match['match_event']} — {match['match_series']}"

                embed = discord.Embed(
                    title=title,
                    description=description,
                    url=url,
                    color=0x5865F2
                )
                embed.set_footer(text=f"{event}\nScores via VLR.gg")

                channel = bot.get_channel(CHANNEL_ID)

                if match_id in match_id_to_message:
                    msg = match_id_to_message[match_id]
                    await msg.edit(embed=embed)
                    match_id_to_embed[match_id] = embed
                else:
                    msg = await channel.send(embed=embed)
                    match_id_to_message[match_id] = msg
                    match_id_to_embed[match_id] = embed

                # If a match is back from ended to live, remove from finalized
                if match_id in finalized_matches:
                    finalized_matches.discard(match_id)

        # Detect ended matches
        active_match_ids = set(match_id_to_message.keys())
        print('Active', active_match_ids, '\n')
        print('Seen', seen_ids, '\n')
        print('Final', finalized_matches)
        ended_matches = active_match_ids - seen_ids

        for match_id in ended_matches:
            if match_id in finalized_matches:
                continue  # Already processed finalization

            elif match_id in match_id_to_message:

                last_map = max(match_history[match_id].keys())

                map_name, r1, r2 = match_history[match_id][last_map]

                score1, score2 = last_known_scores.get(match_id, (r1, r2))

                """if int(r1) > int(r2):
                    r1 = str(int(r1) + 1)
                elif int(r2) > int(r1):
                    r2 = str(int(r2) + 1)"""
                print(r1, r2)
                r1, r2 = fix_round_totals(round_t1=r1, round_t2=r2)
                print(r1, r2)

                if int(r1) > int(r2):
                    score1 += 1
                elif int(r2) > int(r1):
                    score2 += 1

                last_known_scores[match_id] = (int(score1), int(score2))

                match_history[match_id][last_map] = (map_name, r1, r2)

                msg = match_id_to_message[match_id]
                embed = match_id_to_embed[match_id]

                updated_desc = build_match_description(match_id, {
                    "team1": embed.title.split(" vs ")[0],
                    "team2": embed.title.split(" vs ")[1],
                    "score1": score1,
                    "score2": score2,
                    "map_number": str(last_map),
                    "current_map": map_name,
                    "team1_round_t": "0",
                    "team1_round_ct": r1,
                    "team2_round_t": "0",
                    "team2_round_ct": r2,
                }, match_history, status="FINAL")

                embed.description = updated_desc.replace("🎮 Current Score (LIVE)", "🎮 Match Results")

                await msg.edit(embed=embed)

                finalized_matches.add(match_id)


        print('Message Updated @', datetime.now().strftime('%I:%M:%S %p'))

    except requests.exceptions.JSONDecodeError as e:
        with open("payload_error.json", "w", encoding="utf-8") as f:
            f.write(response.text)  # Save raw response
        print(f"[ERROR] Failed to parse JSON: {e}")
        traceback.print_exc()
    except Exception as e:
        print(f"Error updating matches: {e}")
        log_payload_on_exception(response, match_id)
        traceback.print_exc()


@tasks.loop(minutes=10)
async def upcoming_matches():
    try:
        await pause_allowed.wait()

        response = get_upcoming()
        data = response.json()

        data["data"]["segments"] = [segment for segment in data["data"]["segments"] if "VCT" in segment.get("match_event")]

        data = data["data"]["segments"]

        isMoreThan15Min = parse_time_to_seconds(data[0]['time_until_match'])

        if isMoreThan15Min and len(seen_ids) < 1:
            # STOP
            update_allowed.clear()  # pause updates
            print("No incoming or ongoing matches in next 15 min — pausing updates.")

        else:
            # Continue
            update_allowed.set()  # resume updates
            print("Incoming match soon — resuming updates.")

    except Exception as e:
        print(f"Error checking upcoming matches: {e}")
        log_payload_on_exception(data)


@tasks.loop(hours=24)
async def cleanup_final_matches():
    try:
        with open("logs/final_matches.log", "a", encoding="utf-8") as log_file:
            for match_id in list(match_id_to_message.keys()):
                # Check if marked as final
                embed = match_id_to_embed.get(match_id)
                if embed and "🎮 Match Results" in embed.description:
                    log_file.write(f"{datetime.utcnow().isoformat()} - {match_id} FINAL:\n")
                    if match_id in match_history:
                        for i in sorted(match_history[match_id].keys()):
                            map_n, r1, r2 = match_history[match_id][i]
                            log_file.write(f"  Map {i} - {map_n}: {r1} - {r2}\n")
                    log_file.write("\n")

                    # Clean up memory
                    match_id_to_message.pop(match_id, None)
                    match_id_to_embed.pop(match_id, None)
                    match_history.pop(match_id, None)
                    last_known_scores.pop(match_id, None)

        print("[Cleanup] Final matches logged and cleared.")

    except Exception as e:
        print(f"[Cleanup Error] {e}")
        traceback.print_exc()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    update_matches.start()
    cleanup_final_matches.start()
    upcoming_matches.start()

bot.run(TOKEN)
