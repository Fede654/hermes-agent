#!/bin/bash
set -e

API="http://127.0.0.1:50319"
LOGFILE="/tmp/l14_log.txt"
> "$LOGFILE"

log() {
    echo "$1" | tee -a "$LOGFILE"
}

api_get() {
    curl -s "$API$1" 2>/dev/null
}

api_post() {
    curl -s -X POST "$API$1" -H "Content-Type: application/json" -d "$2" 2>/dev/null
}

# Step 1: Initial inventory
log "=== Step 1: Initial Inventory ==="
INV=$(api_get "/inventory")
log "$INV"
START_COOKED=$(echo "$INV" | jq '[.data.items[]? | select(.name == "cooked_beef" or .name == "cooked_porkchop" or .name == "cooked_chicken" or .name == "cooked_mutton" or .name == "cooked_rabbit") | .count] | add // 0')
log "Starting cooked meat: $START_COOKED"

# Step 2: Find passive mobs
log ""
log "=== Step 2: Find Passive Mobs ==="
ANIMALS_KILLED=0
ANIMAL_TYPES=""
RAW_MEAT_COUNTS=""
FURNACE_SOURCE=""
SMELT_RESULT=""
FINAL_COOKED=0

SCAN=0
MAX_SCAN=3

while [ $SCAN -lt $MAX_SCAN ]; do
    SCAN=$((SCAN + 1))
    log "Scan attempt $SCAN..."
    
    # Try type=passive first as instructed
    ENTITIES=$(api_post "/action/find_entities" "{\"type\":\"passive\",\"radius\":32}")
    log "find_entities (passive): $ENTITIES"
    
    # Also try without type filter to get everything
    ENTITIES_ALL=$(api_post "/action/find_entities" "{\"radius\":32}")
    log "find_entities (all): $ENTITIES_ALL"
    
    # Extract target mobs from the unfiltered result
    TARGET_MOBS=$(echo "$ENTITIES_ALL" | jq -c '[.entities[]? | select(.type | test("cow|pig|chicken|sheep|rabbit"; "i"))]')
    MOB_COUNT=$(echo "$TARGET_MOBS" | jq 'length')
    log "Found $MOB_COUNT target mobs"
    
    if [ "$MOB_COUNT" -gt 0 ]; then
        # Process up to 5 mobs or until we have 2+ kills
        MOB_IDX=0
        while [ "$MOB_IDX" -lt "$MOB_COUNT" ] && [ "$ANIMALS_KILLED" -lt 5 ]; do
            MOB=$(echo "$TARGET_MOBS" | jq -c ".[$MOB_IDX]")
            MOB_TYPE=$(echo "$MOB" | jq -r '.type')
            MOB_X=$(echo "$MOB" | jq -r '.position.x // .x')
            MOB_Y=$(echo "$MOB" | jq -r '.position.y // .y')
            MOB_Z=$(echo "$MOB" | jq -r '.position.z // .z')
            
            log "Targeting $MOB_TYPE at $MOB_X,$MOB_Y,$MOB_Z"
            
            # Goto within 2 blocks
            GOTO=$(api_post "/action/goto_near" "{\"x\":$MOB_X,\"y\":$MOB_Y,\"z\":$MOB_Z,\"range\":2}")
            log "goto_near result: $GOTO"
            sleep 2
            
            # Attack up to 5 times
            HITS=0
            MAX_HITS=5
            while [ $HITS -lt $MAX_HITS ]; do
                HITS=$((HITS + 1))
                ATTACK=$(api_post "/action/attack" "{\"target\":\"$MOB_TYPE\"}")
                log "attack $HITS: $ATTACK"
                sleep 1
                
                # Check if entity is still around
                CHECK=$(api_post "/action/find_entities" "{\"type\":\"$MOB_TYPE\",\"radius\":8}")
                STILL_THERE=$(echo "$CHECK" | jq '[.entities[]?] | length')
                if [ "$STILL_THERE" -eq 0 ]; then
                    log "$MOB_TYPE killed!"
                    ANIMALS_KILLED=$((ANIMALS_KILLED + 1))
                    ANIMAL_TYPES="$ANIMAL_TYPES $MOB_TYPE"
                    break
                fi
            done
            
            # Pickup drops
            PICKUP=$(api_post "/action/pickup" "{}")
            log "pickup: $PICKUP"
            sleep 1
            
            MOB_IDX=$((MOB_IDX + 1))
            
            if [ "$ANIMALS_KILLED" -ge 2 ]; then
                break
            fi
        done
    fi
    
    if [ "$ANIMALS_KILLED" -ge 2 ]; then
        break
    fi
    
    # Move 10 blocks in random direction if no kills yet
    if [ "$ANIMALS_KILLED" -lt 2 ] && [ "$SCAN" -lt "$MAX_SCAN" ]; then
        # Get current position
        STATUS=$(api_get "/status")
        CUR_X=$(echo "$STATUS" | jq -r '.data.position.x')
        CUR_Z=$(echo "$STATUS" | jq -r '.data.position.z')
        # Pick random direction: N/S/E/W
        DIRS=("north" "south" "east" "west")
        DIR_IDX=$((RANDOM % 4))
        case ${DIRS[$DIR_IDX]} in
            north) NEW_Z=$(echo "$CUR_Z - 10" | bc) ; NEW_X=$CUR_X ;;
            south) NEW_Z=$(echo "$CUR_Z + 10" | bc) ; NEW_X=$CUR_X ;;
            east)  NEW_X=$(echo "$CUR_X + 10" | bc) ; NEW_Z=$CUR_Z ;;
            west)  NEW_X=$(echo "$CUR_X - 10" | bc) ; NEW_Z=$CUR_Z ;;
        esac
        CUR_Y=$(echo "$STATUS" | jq -r '.data.position.y')
        log "Moving 10 blocks ${DIRS[$DIR_IDX]} to $NEW_X,$CUR_Y,$NEW_Z"
        MOVE=$(api_post "/action/goto" "{\"x\":$NEW_X,\"y\":$CUR_Y,\"z\":$NEW_Z}")
        log "move result: $MOVE"
        sleep 5
    fi
done

log ""
log "Total animals killed: $ANIMALS_KILLED ($ANIMAL_TYPES)"

if [ "$ANIMALS_KILLED" -lt 2 ]; then
    log "L14_FAIL: no passive mobs in env"
    REPORT="\n## L14 — Collect + Cook Food (today)\n- animals killed: $ANIMALS_KILLED ($ANIMAL_TYPES)\n- raw meat collected: none\n- furnace found or crafted: N/A\n- smelting result: N/A\n- final cooked meat count: $START_COOKED\n"
    mkdir -p /home/fede/REPOS/vault/raw/hermes-repo
    echo -e "$REPORT" >> /home/fede/REPOS/vault/raw/hermes-repo/altercraft-epic-progress.md
    log "DONE_L14"
    exit 0
fi

# Step 4: Check raw meat
log ""
log "=== Step 4: Check Raw Meat ==="
INV2=$(api_get "/inventory")
log "$INV2"

RAW_BEEF=$(echo "$INV2" | jq '[.data.items[]? | select(.name == "raw_beef") | .count] | add // 0')
RAW_PORK=$(echo "$INV2" | jq '[.data.items[]? | select(.name == "raw_porkchop") | .count] | add // 0')
RAW_CHICKEN=$(echo "$INV2" | jq '[.data.items[]? | select(.name == "raw_chicken") | .count] | add // 0')
RAW_MUTTON=$(echo "$INV2" | jq '[.data.items[]? | select(.name == "raw_mutton") | .count] | add // 0')
RAW_RABBIT=$(echo "$INV2" | jq '[.data.items[]? | select(.name == "raw_rabbit") | .count] | add // 0')

log "Raw beef: $RAW_BEEF, pork: $RAW_PORK, chicken: $RAW_CHICKEN, mutton: $RAW_MUTTON, rabbit: $RAW_RABBIT"

# Determine best raw meat to smelt
BEST_RAW=""
BEST_COUNT=0
for pair in "raw_beef:$RAW_BEEF" "raw_porkchop:$RAW_PORK" "raw_chicken:$RAW_CHICKEN" "raw_mutton:$RAW_MUTTON" "raw_rabbit:$RAW_RABBIT"; do
    ITEM=${pair%%:*}
    COUNT=${pair##*:}
    if [ "$COUNT" -gt "$BEST_COUNT" ]; then
        BEST_COUNT=$COUNT
        BEST_RAW=$ITEM
    fi
done

RAW_MEAT_COUNTS="raw_beef=$RAW_BEEF raw_porkchop=$RAW_PORK raw_chicken=$RAW_CHICKEN raw_mutton=$RAW_MUTTON raw_rabbit=$RAW_RABBIT"

if [ -z "$BEST_RAW" ] || [ "$BEST_COUNT" -eq 0 ]; then
    log "L14_FAIL: no raw meat collected"
    REPORT="\n## L14 — Collect + Cook Food (today)\n- animals killed: $ANIMALS_KILLED ($ANIMAL_TYPES)\n- raw meat collected: $RAW_MEAT_COUNTS\n- furnace found or crafted: N/A\n- smelting result: N/A\n- final cooked meat count: $START_COOKED\n"
    mkdir -p /home/fede/REPOS/vault/raw/hermes-repo
    echo -e "$REPORT" >> /home/fede/REPOS/vault/raw/hermes-repo/altercraft-epic-progress.md
    log "DONE_L14"
    exit 0
fi

COUNT_TO_SMELT=5
if [ "$BEST_COUNT" -lt 5 ]; then
    COUNT_TO_SMELT=$BEST_COUNT
fi
log "Will smelt $COUNT_TO_SMELT of $BEST_RAW"

# Step 5: Find furnace
log ""
log "=== Step 5: Find Furnace ==="
FURNACES=$(api_post "/action/find_blocks" "{\"block\":\"furnace\",\"radius\":16,\"count\":5}")
log "find_blocks furnace: $FURNACES"
FURNACE_POS=$(echo "$FURNACES" | jq -c '.locations[0]? // empty')

if [ -z "$FURNACE_POS" ]; then
    log "No furnace found, need to craft one..."
    FURNACE_SOURCE="crafted"
    
    # Find cobblestone
    COBBLE=$(api_post "/action/find_blocks" "{\"block\":\"cobblestone\",\"radius\":16,\"count\":10}")
    log "find_blocks cobblestone: $COBBLE"
    COBBLE_LOCS=$(echo "$COBBLE" | jq -c '.locations[]? // empty')
    
    COBBLE_DUG=0
    if [ -n "$COBBLE_LOCS" ]; then
        for loc in $(echo "$COBBLE" | jq -c '.locations[]? | {x,y,z}' | head -8); do
            if [ "$COBBLE_DUG" -ge 8 ]; then break; fi
            CX=$(echo "$loc" | jq -r '.x')
            CY=$(echo "$loc" | jq -r '.y')
            CZ=$(echo "$loc" | jq -r '.z')
            log "Digging cobblestone at $CX,$CY,$CZ"
            DIG=$(api_post "/action/dig" "{\"x\":$CX,\"y\":$CY,\"z\":$CZ}")
            log "dig result: $DIG"
            COBBLE_DUG=$((COBBLE_DUG + 1))
            sleep 1
        done
    fi
    
    # Check inventory for cobblestone
    INV3=$(api_get "/inventory")
    COBBLE_COUNT=$(echo "$INV3" | jq '[.data.items[]? | select(.name == "cobblestone") | .count] | add // 0')
    log "Cobblestone in inventory: $COBBLE_COUNT"
    
    if [ "$COBBLE_COUNT" -ge 8 ]; then
        log "Crafting furnace..."
        CRAFT=$(api_post "/action/craft" "{\"item\":\"furnace\",\"count\":1}")
        log "craft result: $CRAFT"
        sleep 1
        
        # Get current position for placing
        STATUS=$(api_get "/status")
        PX=$(echo "$STATUS" | jq -r '.data.position.x')
        PY=$(echo "$STATUS" | jq -r '.data.position.y')
        PZ=$(echo "$STATUS" | jq -r '.data.position.z')
        PLACE_X=$(echo "$PX + 1" | bc)
        PLACE_Y=$PY
        PLACE_Z=$PZ
        log "Placing furnace at $PLACE_X,$PLACE_Y,$PLACE_Z"
        PLACE=$(api_post "/action/place" "{\"block\":\"furnace\",\"x\":$PLACE_X,\"y\":$PLACE_Y,\"z\":$PLACE_Z}")
        log "place result: $PLACE"
        sleep 1
        
        # Verify furnace is there
        FURNACES2=$(api_post "/action/find_blocks" "{\"block\":\"furnace\",\"radius\":4,\"count\":5}")
        FURNACE_POS=$(echo "$FURNACES2" | jq -c '.locations[0]? // empty')
    else
        log "L14_FAIL: could not gather enough cobblestone ($COBBLE_COUNT/8)"
        REPORT="\n## L14 — Collect + Cook Food (today)\n- animals killed: $ANIMALS_KILLED ($ANIMAL_TYPES)\n- raw meat collected: $RAW_MEAT_COUNTS\n- furnace found or crafted: failed (only $COBBLE_COUNT cobblestone)\n- smelting result: N/A\n- final cooked meat count: $START_COOKED\n"
        mkdir -p /home/fede/REPOS/vault/raw/hermes-repo
        echo -e "$REPORT" >> /home/fede/REPOS/vault/raw/hermes-repo/altercraft-epic-progress.md
        log "DONE_L14"
        exit 0
    fi
else
    FURNACE_SOURCE="found"
    log "Using furnace at $FURNACE_POS"
fi

if [ -z "$FURNACE_POS" ]; then
    log "L14_FAIL: no furnace available"
    REPORT="\n## L14 — Collect + Cook Food (today)\n- animals killed: $ANIMALS_KILLED ($ANIMAL_TYPES)\n- raw meat collected: $RAW_MEAT_COUNTS\n- furnace found or crafted: failed\n- smelting result: N/A\n- final cooked meat count: $START_COOKED\n"
    mkdir -p /home/fede/REPOS/vault/raw/hermes-repo
    echo -e "$REPORT" >> /home/fede/REPOS/vault/raw/hermes-repo/altercraft-epic-progress.md
    log "DONE_L14"
    exit 0
fi

FX=$(echo "$FURNACE_POS" | jq -r '.x')
FY=$(echo "$FURNACE_POS" | jq -r '.y')
FZ=$(echo "$FURNACE_POS" | jq -r '.z')

# Step 6: Smelting
log ""
log "=== Step 6: Smelting ==="
log "Starting smelt of $COUNT_TO_SMELT $BEST_RAW..."
SMELT=$(api_post "/action/smelt_start" "{\"input\":\"$BEST_RAW\",\"count\":$COUNT_TO_SMELT}")
log "smelt_start result: $SMELT"
SMELT_RESULT="smelted $COUNT_TO_SMELT $BEST_RAW"

# Step 7: Wait
log ""
log "=== Step 7: Waiting 45 seconds ==="
sleep 45

# Step 8: Collect from furnace
log ""
log "=== Step 8: Collect from furnace ==="
TAKE=$(api_post "/action/furnace_take" "{\"x\":$FX,\"y\":$FY,\"z\":$FZ}")
log "furnace_take result: $TAKE"

# Also try pickup
PICKUP2=$(api_post "/action/pickup" "{}")
log "pickup after smelt: $PICKUP2"

# Step 9: Final inventory
log ""
log "=== Step 9: Final Inventory ==="
FINAL=$(api_get "/inventory")
log "$FINAL"

FINAL_COOKED=$(echo "$FINAL" | jq '[.data.items[]? | select(.name == "cooked_beef" or .name == "cooked_porkchop" or .name == "cooked_chicken" or .name == "cooked_mutton" or .name == "cooked_rabbit") | .count] | add // 0')
log "Final cooked meat count: $FINAL_COOKED"

# Build report
REPORT="\n## L14 — Collect + Cook Food (today)\n- animals killed: $ANIMALS_KILLED ($ANIMAL_TYPES)\n- raw meat collected: $RAW_MEAT_COUNTS\n- furnace found or crafted: $FURNACE_SOURCE\n- smelting result: $SMELT_RESULT\n- final cooked meat count: $FINAL_COOKED\n"

mkdir -p /home/fede/REPOS/vault/raw/hermes-repo
echo -e "$REPORT" >> /home/fede/REPOS/vault/raw/hermes-repo/altercraft-epic-progress.md

log ""
log "Report appended."

if [ "$FINAL_COOKED" -ge 5 ] && [ "$ANIMALS_KILLED" -ge 2 ]; then
    log "L14_PASS"
else
    log "L14_FAIL: final cooked meat = $FINAL_COOKED, animals killed = $ANIMALS_KILLED"
fi

log "DONE_L14"
