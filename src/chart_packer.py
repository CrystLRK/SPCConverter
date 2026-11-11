import json
import struct
import sys
from pathlib import Path

from chart_validation import ChartValidationError, validate_standard_chart
from icp1_codec import (
    State,
    advance_note,
    mask_for_field,
    positive_gcd,
    s32,
    seed_state,
)
from showtext_validation import validate_showtext_standard
from spc_v0_chart_decoder import read_spc_v0_chart

SPC_HEADER_SIZE = 28
SPC_NOTE_SIZE = 80
SPC_EVENT_SIZE = 32
TYPE_FLICK = 4
TYPE_FIELD = 5

def read_json(file_name: str) -> dict:
    with open(file_name, 'r', encoding='utf-8') as f:
        return json.load(f)
    
def write_json(file_name: str, obj) -> None:
    with open(file_name, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=4)
        
def write_spc(file_name: str, data: bytearray) -> None:
    with open(file_name, 'wb') as f:
        f.write(data)

def encode_note_block(note: dict, state: State, index: int) -> bytearray:
    #还原fields
    note_block_data: bytearray = bytearray(SPC_NOTE_SIZE)
    if "flags" in note:
        # 新语句（bomb / trackmove）以及任何显式给出 flags 的音符直接使用该值
        field5 = int(note["flags"]) & 0xFFFFFFFF
    elif note["type"] == TYPE_FLICK:
        field5 = note["flick"] << 8
    elif note["type"] == TYPE_FIELD:
        field5 = 0
        match note["leftEase"]:
            case 0:
                field5 |= 0b00000100
            case 1:
                field5 |= 0b00001000
            case 2:
                field5 |= 0b00010000
        match note["rightEase"]:
            case 0:
                field5 |= 0b00100000
            case 1:
                field5 |= 0b01000000
            case 2:
                field5 |= 0b10000000
        #field5 = ([0, 1, 2][note["leftEase"]] << 4) | [0, 1, 2][note["rightEase"]]
    else:
        field5 = 0
    fields: list[int] = [
        note["index"],
        note["line"],
        note["timeMs"],
        note["endMs"],
        (note["type"] << 8) | note["track"],
        field5,
        0,
        0,
        0,
        0
    ]
    #print(fields)
    for k, off, size in (
        [
        (0, 0x00, 8),
        (1, 0x08, 8),
        (2, 0x10, 4),
        (3, 0x14, 4),
        (4, 0x18, 4),
        (5, 0x1C, 4)
        ]
    ):
        #print(note,k)
        #print(fields)
        #print(fields[k])
        #print(gen(state, index, k))
        cipher: int = (
            fields[k] + mask_for_field(state, index, k)
        ) & ((1 << (8 * size)) - 1)
        #print(cipher)
        note_block_data[off:off + size] = cipher.to_bytes(size, 'little')
    #plain这段我是真几把不知道怎么写了，直接把fractions的数据拿过来吧
    #onp真行啊？
    plains: list[int] = [
        note["fractions"][0][0],
        note["fractions"][0][1],
        note["fractions"][1][0],
        note["fractions"][1][1],
        note["fractions"][2][0],
        note["fractions"][2][1],
        note["fractions"][3][0],
        note["fractions"][3][1]
    ]
    #逆推field6到9的值
    for i, j in zip(range(0, 8, 2), range(6, 10)):
        g = positive_gcd(s32(plains[i]), s32(plains[i + 1]))
        fields[j] = g
    for k, off, size in (
        [
        (6, 0x20, 4),
        (7, 0x24, 4),
        (8, 0x28, 4),
        (9, 0x2C, 4)
        ]
    ):
        cipher: int = (
            fields[k] + mask_for_field(state, index, k)
        ) & ((1 << (8 * size)) - 1)
        note_block_data[off:off + size] = cipher.to_bytes(size, 'little')
    #print(plains)
    note_block_data[48:80] = struct.pack('<8i', *plains)
    return (note_block_data, fields, plains)

def encode_event_block(event: dict) -> bytearray:
    event_id = event["id"]
    time_ms = event["timeMs"]
    event_type = event["type"]
    if event_type == 3:
        value1 = event["flags"]
    else:
        # type1 区段 / type2 过渡（demo BpmChangeV3）用 "bpm"；
        # type0 流速 / type2 noinput 用 "value"（两者都是 double）。
        number = event["bpm"] if "bpm" in event else event["value"]
        tmp_tuple = number
        value1 = struct.unpack("<Q", struct.pack("<d", tmp_tuple))[0]
    if "speed" in event:
        tmp_tuple = event["speed"]
        value2 = struct.unpack("<I", struct.pack("<f", tmp_tuple))[0]
    else:
        value2 = 0
    event_block_data: bytearray = bytearray(SPC_EVENT_SIZE)
    """event_block_data = [
        event_id.to_bytes(8, 'little'),
        time_ms.to_bytes(4, 'little'),
        event_type.to_bytes(4, 'little'),
        value1.to_bytes(8, 'little'),
        value2.to_bytes(8, 'little')
    ]"""
    event_block_data = struct.pack("<QIIQQ", event_id, time_ms, event_type, value1, value2)
    return event_block_data
        
def pack_chart(chart_data_json: dict, chart_name: str) -> bytearray:
    try:
        validate_standard_chart(chart_data_json)
    except ChartValidationError as exc:
        raise ValueError(f"打包前输入域校验失败：{exc}") from exc
    if chart_data_json["format"] != "ICP1":
        print("Invalid format!")
        sys.exit(1)
    else:
        chart_len = SPC_HEADER_SIZE + len(chart_data_json["notes"]) * SPC_NOTE_SIZE + len(chart_data_json["events"]) * SPC_EVENT_SIZE
        note_count = chart_data_json["noteCount"]
        event_count = chart_data_json["eventCount"]
        bpm = chart_data_json["bpm"]
        meter = chart_data_json["meter"]
        icp1_chart_data = bytearray(chart_len)
        struct.pack_into('4s', icp1_chart_data, 0x0, b'ICP1')
        struct.pack_into('<HHHHII', icp1_chart_data, 0x4, 
                         chart_data_json["version"],
                         SPC_HEADER_SIZE,
                         SPC_NOTE_SIZE,
                         SPC_EVENT_SIZE,
                         note_count,
                         event_count
        )
        struct.pack_into("<ff", icp1_chart_data, 0x14, 
                         bpm, 
                         meter
        )
        seed = chart_name #种子为谱面文件全名，包含后缀名（xxx.spc)
        state = seed_state(seed, note_count)
        for i in range(note_count):
            pointer_1 = SPC_HEADER_SIZE + i * SPC_NOTE_SIZE
            pointer_2 = SPC_HEADER_SIZE + (i + 1) * SPC_NOTE_SIZE
            (note_block, fields, plains) = encode_note_block(chart_data_json["notes"][i], state, i)
            scaled = [
                value
                for pair in chart_data_json["notes"][i]["fractions"]
                for value in pair
            ]
            advance_note(
                state,
                fields[0], fields[1], fields[2], fields[3],
                fields[4], fields[5], scaled, plains, i,
            )
            if len(note_block) != SPC_NOTE_SIZE:
                print(f"Note block size mismatch: expected {SPC_NOTE_SIZE}, got {len(note_block)}")
                input()
            else:
                icp1_chart_data[pointer_1:pointer_2] = note_block
        for i in range(event_count):
            pointer_1 = SPC_HEADER_SIZE + note_count * SPC_NOTE_SIZE + i * SPC_EVENT_SIZE
            pointer_2 = SPC_HEADER_SIZE + note_count * SPC_NOTE_SIZE + (i + 1) * SPC_EVENT_SIZE
            event = chart_data_json["events"][i]
            event_block = encode_event_block(event)
            if len(event_block) != SPC_EVENT_SIZE:
                print(f"Event block size mismatch: expected {SPC_EVENT_SIZE}, got {len(event_block)}")
                print(event_block)
                print(pointer_1, pointer_2)
                icp1_chart_data[pointer_1:pointer_2] = event_block
                #input()
            else:
                icp1_chart_data[pointer_1:pointer_2] = event_block
    return icp1_chart_data
    

        
def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Compatibility CLI for standard JSON / SPC V0 to SPC V1."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--json2spcV1", action="store_true", dest="json_to_spc")
    mode.add_argument("--spcV02spcV1", action="store_true", dest="spc0_to_spc1")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path, nargs="?")
    args = parser.parse_args(argv)

    source = args.input.resolve()
    if not source.is_file():
        parser.error(f"input file does not exist: {source}")

    if args.json_to_spc:
        output = args.output or source.with_suffix(".spc")
        chart = read_json(source)
    else:
        output = args.output or Path("output") / source.name
        chart = read_spc_v0_chart(str(source))
    output = output.resolve()
    validate_showtext_standard(chart, source)
    final_data = pack_chart(chart, output.name)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_spc(output, final_data)
    mode_name = "--json2spcV1" if args.json_to_spc else "--spcV02spcV1"
    print(f"[{mode_name}] {output} Packed")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
