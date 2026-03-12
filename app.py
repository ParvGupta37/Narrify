from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import json
import os

app = Flask(__name__)
CORS(app)

DB = "database.db"

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


# ── Stories ────────────────────────────────────────────────

@app.route("/stories", methods=["GET"])
def get_stories():
    conn = get_db()
    stories = conn.execute("SELECT * FROM stories").fetchall()
    conn.close()
    return jsonify([dict(s) for s in stories])


@app.route("/stories", methods=["POST"])
def create_story():
    data = request.json
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO stories (title, description) VALUES (?, ?)",
        (data["title"], data.get("description", ""))
    )
    conn.commit()
    story_id = cursor.lastrowid
    conn.close()
    return jsonify({"id": story_id}), 201


@app.route("/stories/<int:story_id>/start", methods=["GET"])
def start_story(story_id):
    conn = get_db()
    story = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    if not story or not story["start_scene_id"]:
        conn.close()
        return jsonify({"error": "Story not found or has no starting scene"}), 404
    scene = conn.execute("SELECT * FROM scenes WHERE id = ?", (story["start_scene_id"],)).fetchone()
    all_choices = conn.execute("SELECT * FROM choices WHERE scene_id = ?", (scene["id"],)).fetchall()
    variables = conn.execute(
        "SELECT name, default_value, var_type FROM story_variables WHERE story_id = ?", (story_id,)
    ).fetchall()
    conn.close()

    default_state = {}
    for v in variables:
        val = v["default_value"]
        if v["var_type"] == "number":
            val = float(val) if "." in str(val) else int(val)
        elif v["var_type"] == "bool":
            val = val.lower() == "true"
        default_state[v["name"]] = val

    return jsonify({
        "scene": dict(scene),
        "choices": [dict(c) for c in all_choices],
        "default_state": default_state
    })


@app.route("/stories/<int:story_id>/set_start", methods=["POST"])
def set_start_scene(story_id):
    data = request.json
    conn = get_db()
    conn.execute("UPDATE stories SET start_scene_id = ? WHERE id = ?", (data["scene_id"], story_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Scenes ─────────────────────────────────────────────────

@app.route("/stories/<int:story_id>/scenes", methods=["GET"])
def get_scenes(story_id):
    conn = get_db()
    scenes = conn.execute("SELECT * FROM scenes WHERE story_id = ?", (story_id,)).fetchall()
    conn.close()
    return jsonify([dict(s) for s in scenes])


@app.route("/scenes", methods=["POST"])
def create_scene():
    data = request.json
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO scenes (story_id, narrative, is_ending) VALUES (?, ?, ?)",
        (data["story_id"], data["narrative"], data.get("is_ending", 0))
    )
    conn.commit()
    scene_id = cursor.lastrowid
    conn.close()
    return jsonify({"id": scene_id}), 201


@app.route("/scenes/<int:scene_id>", methods=["GET"])
def get_scene(scene_id):
    conn = get_db()
    scene = conn.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone()
    if not scene:
        conn.close()
        return jsonify({"error": "Scene not found"}), 404
    choices = conn.execute("SELECT * FROM choices WHERE scene_id = ?", (scene_id,)).fetchall()
    conn.close()
    return jsonify({
        "scene": dict(scene),
        "choices": [dict(c) for c in choices]
    })


# ── Choices ────────────────────────────────────────────────

@app.route("/choices", methods=["POST"])
def create_choice():
    data = request.json
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO choices (scene_id, label, next_scene_id, condition, effects) VALUES (?, ?, ?, ?, ?)",
        (
            data["scene_id"],
            data["label"],
            data["next_scene_id"],
            data.get("condition", ""),
            data.get("effects", "")
        )
    )
    conn.commit()
    choice_id = cursor.lastrowid
    conn.close()
    return jsonify({"id": choice_id}), 201


# ── Story Engine: select a choice, apply effects, return next scene ──

@app.route("/choices/<int:choice_id>/select", methods=["POST"])
def select_choice(choice_id):
    data = request.json or {}
    state = data.get("state", {})

    conn = get_db()
    choice = conn.execute("SELECT * FROM choices WHERE id = ?", (choice_id,)).fetchone()
    if not choice:
        conn.close()
        return jsonify({"error": "Choice not found"}), 404

    updated_state = apply_effects(state, choice["effects"] or "")

    next_scene_id = choice["next_scene_id"]
    scene = conn.execute("SELECT * FROM scenes WHERE id = ?", (next_scene_id,)).fetchone()
    all_choices = conn.execute("SELECT * FROM choices WHERE scene_id = ?", (next_scene_id,)).fetchall()
    conn.close()

    available_choices = []
    for c in all_choices:
        cond = c["condition"] or ""
        if not cond or evaluate_condition(cond, updated_state):
            available_choices.append(dict(c))

    return jsonify({
        "scene": dict(scene),
        "choices": available_choices,
        "state": updated_state
    })


# ── Engine helpers ─────────────────────────────────────────

def evaluate_condition(condition_str, state):
    if not condition_str or not condition_str.strip():
        return True
    try:
        for op in [">=", "<=", "!=", "==", ">", "<"]:
            if op in condition_str:
                left, right = condition_str.split(op, 1)
                var_name = left.strip()
                raw_value = right.strip()
                current = state.get(var_name)
                if current is None:
                    return False
                if raw_value.lower() == "true":    compare_val = True
                elif raw_value.lower() == "false": compare_val = False
                else:
                    try:
                        compare_val = float(raw_value) if "." in raw_value else int(raw_value)
                    except ValueError:
                        compare_val = raw_value
                if op == "==":  return current == compare_val
                if op == "!=":  return current != compare_val
                if op == ">=":  return current >= compare_val
                if op == "<=":  return current <= compare_val
                if op == ">":   return current > compare_val
                if op == "<":   return current < compare_val
    except Exception:
        return True
    return True


def apply_effects(state, effects_str):
    updated = dict(state)
    if not effects_str or not effects_str.strip():
        return updated
    for effect in effects_str.split(";"):
        effect = effect.strip()
        if not effect:
            continue
        try:
            if "+=" in effect:
                var, val = effect.split("+=", 1)
                updated[var.strip()] = updated.get(var.strip(), 0) + _parse_val(val.strip())
            elif "-=" in effect:
                var, val = effect.split("-=", 1)
                updated[var.strip()] = updated.get(var.strip(), 0) - _parse_val(val.strip())
            elif "=" in effect:
                var, val = effect.split("=", 1)
                updated[var.strip()] = _parse_val(val.strip())
        except Exception:
            continue
    return updated


def _parse_val(raw):
    if raw.lower() == "true":  return True
    if raw.lower() == "false": return False
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return raw


# ── Story Variables ────────────────────────────────────────

@app.route("/stories/<int:story_id>/variables", methods=["GET"])
def get_variables(story_id):
    conn = get_db()
    rows = conn.execute("SELECT * FROM story_variables WHERE story_id = ?", (story_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/stories/<int:story_id>/variables", methods=["POST"])
def create_variable(story_id):
    data = request.json
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO story_variables (story_id, name, default_value, var_type) VALUES (?, ?, ?, ?)",
        (story_id, data["name"], str(data["default_value"]), data.get("var_type", "number"))
    )
    conn.commit()
    conn.close()
    return jsonify({"id": cursor.lastrowid}), 201


# ── Progress ───────────────────────────────────────────────

@app.route("/progress", methods=["POST"])
def save_progress():
    data = request.json
    conn = get_db()
    state_json = json.dumps(data.get("state", {}))
    existing = conn.execute(
        "SELECT id FROM player_progress WHERE session_key = ? AND story_id = ?",
        (data["session_key"], data["story_id"])
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE player_progress SET current_scene_id = ?, state_json = ? WHERE id = ?",
            (data["scene_id"], state_json, existing["id"])
        )
    else:
        conn.execute(
            "INSERT INTO player_progress (story_id, current_scene_id, session_key, state_json) VALUES (?, ?, ?, ?)",
            (data["story_id"], data["scene_id"], data["session_key"], state_json)
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/progress/<session_key>/<int:story_id>", methods=["GET"])
def load_progress(session_key, story_id):
    conn = get_db()
    row = conn.execute(
        "SELECT current_scene_id, state_json FROM player_progress WHERE session_key = ? AND story_id = ?",
        (session_key, story_id)
    ).fetchone()
    conn.close()
    if row:
        state = json.loads(row["state_json"]) if row["state_json"] else {}
        return jsonify({"scene_id": row["current_scene_id"], "state": state})
    return jsonify({"scene_id": None, "state": {}})


# ── Delete ─────────────────────────────────────────────────

@app.route("/stories/<int:story_id>", methods=["DELETE"])
def delete_story(story_id):
    conn = get_db()
    # Delete in dependency order
    conn.execute("DELETE FROM player_progress WHERE story_id = ?", (story_id,))
    conn.execute("""
        DELETE FROM choices WHERE scene_id IN
        (SELECT id FROM scenes WHERE story_id = ?)
    """, (story_id,))
    conn.execute("DELETE FROM scenes WHERE story_id = ?", (story_id,))
    conn.execute("DELETE FROM story_variables WHERE story_id = ?", (story_id,))
    conn.execute("DELETE FROM stories WHERE id = ?", (story_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Export ─────────────────────────────────────────────────

@app.route("/stories/<int:story_id>/export", methods=["GET"])
def export_story(story_id):
    conn = get_db()
    story = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    if not story:
        conn.close()
        return jsonify({"error": "Story not found"}), 404

    scenes = conn.execute("SELECT * FROM scenes WHERE story_id = ?", (story_id,)).fetchall()
    variables = conn.execute("SELECT * FROM story_variables WHERE story_id = ?", (story_id,)).fetchall()

    scene_list = []
    for scene in scenes:
        choices = conn.execute("SELECT * FROM choices WHERE scene_id = ?", (scene["id"],)).fetchall()
        scene_list.append({
            "scene_key": f"scene_{scene['id']}",
            "narrative": scene["narrative"],
            "is_ending": bool(scene["is_ending"]),
            "choices": [
                {
                    "label": c["label"],
                    "next_scene_key": f"scene_{c['next_scene_id']}",
                    "condition": c["condition"] or "",
                    "effects": c["effects"] or ""
                }
                for c in choices
            ]
        })

    conn.close()
    return jsonify({
        "title": story["title"],
        "description": story["description"],
        "start_scene_key": f"scene_{story['start_scene_id']}",
        "variables": [
            {"name": v["name"], "default_value": v["default_value"], "type": v["var_type"]}
            for v in variables
        ],
        "scenes": scene_list
    })


# ── Import ─────────────────────────────────────────────────

@app.route("/stories/import", methods=["POST"])
def import_story():
    data = request.json
    if not data or "title" not in data or "scenes" not in data:
        return jsonify({"error": "Invalid story format. Need title and scenes."}), 400

    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO stories (title, description) VALUES (?, ?)",
        (data["title"], data.get("description", ""))
    )
    story_id = cursor.lastrowid

    for v in data.get("variables", []):
        conn.execute(
            "INSERT INTO story_variables (story_id, name, default_value, var_type) VALUES (?, ?, ?, ?)",
            (story_id, v["name"], str(v.get("default_value", "0")), v.get("type", "number"))
        )

    key_to_id = {}
    scene_choices_pending = []

    for scene_data in data["scenes"]:
        cur = conn.execute(
            "INSERT INTO scenes (story_id, narrative, is_ending) VALUES (?, ?, ?)",
            (story_id, scene_data["narrative"], 1 if scene_data.get("is_ending") else 0)
        )
        real_id = cur.lastrowid
        key = scene_data.get("scene_key", str(real_id))
        key_to_id[key] = real_id
        scene_choices_pending.append((real_id, scene_data.get("choices", [])))

    for scene_id, choices in scene_choices_pending:
        for c in choices:
            next_id = key_to_id.get(c.get("next_scene_key", ""))
            if next_id is None:
                continue
            conn.execute(
                "INSERT INTO choices (scene_id, label, next_scene_id, condition, effects) VALUES (?, ?, ?, ?, ?)",
                (scene_id, c["label"], next_id, c.get("condition", ""), c.get("effects", ""))
            )

    start_id = key_to_id.get(data.get("start_scene_key", ""))
    if start_id:
        conn.execute("UPDATE stories SET start_scene_id = ? WHERE id = ?", (start_id, story_id))

    conn.commit()
    conn.close()
    return jsonify({"id": story_id, "title": data["title"]}), 201


# ── Validate ───────────────────────────────────────────────

@app.route("/stories/<int:story_id>/validate", methods=["GET"])
def validate_story(story_id):
    conn = get_db()
    story = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    if not story:
        conn.close()
        return jsonify({"error": "Story not found"}), 404

    scenes = conn.execute("SELECT * FROM scenes WHERE story_id = ?", (story_id,)).fetchall()
    choices = conn.execute(
        "SELECT c.* FROM choices c JOIN scenes s ON c.scene_id = s.id WHERE s.story_id = ?", (story_id,)
    ).fetchall()
    conn.close()

    issues = []
    scene_ids = {s["id"] for s in scenes}
    reachable = set()

    if not story["start_scene_id"]:
        issues.append({"type": "error", "message": "No starting scene is set."})
    else:
        reachable.add(story["start_scene_id"])

    for c in choices:
        reachable.add(c["next_scene_id"])
        if c["next_scene_id"] not in scene_ids:
            issues.append({"type": "error", "message": f"Choice '{c['label']}' points to a missing scene."})

    choice_scene_ids = {c["scene_id"] for c in choices}
    for s in scenes:
        if not s["is_ending"] and s["id"] not in choice_scene_ids:
            issues.append({"type": "warning", "message": f"Scene '{s['narrative'][:45]}…' has no choices and is not marked as an ending."})
        if s["id"] not in reachable:
            issues.append({"type": "warning", "message": f"Scene '{s['narrative'][:45]}…' is unreachable from any other scene."})

    return jsonify({
        "valid": not any(i["type"] == "error" for i in issues),
        "scene_count": len(scenes),
        "choice_count": len(choices),
        "issues": issues
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)