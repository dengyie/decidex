#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_PY="$SCRIPT_DIR/.venv/bin/python"

if [ ! -f "$VENV_PY" ]; then
    echo "❌ Error: Virtual environment not found at $VENV_PY"
    exit 1
fi

export PYTHONPATH="$SCRIPT_DIR/src:$PYTHONPATH"
export DECIDEX_KEY_FILE="${DECIDEX_KEY_FILE:-$SCRIPT_DIR/keys.jsonl}"

case "$1" in
    stats)
        "$VENV_PY" -m decidex.tools.pool_manager stats "${@:2}"
        ;;
    probe)
        "$VENV_PY" -m decidex.tools.pool_manager probe "${@:2}"
        ;;
    export)
        "$VENV_PY" -m decidex.tools.pool_manager export "${@:2}"
        ;;
    test)
        "$SCRIPT_DIR/.venv/bin/pytest" -v tests "${@:2}"
        ;;
    play-2048)
        "$VENV_PY" examples/play_2048_live.py "${@:2}"
        ;;
    demo)
        DEMO_NAME="${2:-mario}"
        case "$DEMO_NAME" in
            mario)
                "$VENV_PY" examples/mario_reactive_demo.py
                ;;
            balatro)
                "$VENV_PY" examples/balatro_turn_demo.py
                ;;
            2048)
                "$VENV_PY" examples/game2048_lookahead_demo.py
                ;;
            keypool)
                "$VENV_PY" examples/keypool_rotation_demo.py
                ;;
            *)
                echo "Unknown demo: $DEMO_NAME (available: mario, balatro, 2048, keypool)"
                exit 1
                ;;
        esac
        ;;
    *)
        echo "Usage: ./manage.sh {stats|probe|export|test|play-2048|demo} [options]"
        echo "Examples:"
        echo "  ./manage.sh stats"
        echo "  ./manage.sh probe --sample 5 --workers 5"
        echo "  ./manage.sh test"
        echo "  ./manage.sh play-2048 --mode hybrid --delay 0.05"
        echo "  ./manage.sh demo mario"
        echo "  ./manage.sh demo keypool"
        exit 1
        ;;
esac
