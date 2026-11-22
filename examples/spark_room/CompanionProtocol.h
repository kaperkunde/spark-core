#pragma once

// Frame codes of the MeshCore companion protocol, copied from
// examples/companion_radio/MyMesh.cpp (upstream keeps them private to that .cpp).
// Only the subset HostLink serves is listed. Keep numeric values identical.

// host -> node
#define CMD_APP_START                 1
#define CMD_SEND_TXT_MSG              2
#define CMD_SEND_CHANNEL_TXT_MSG      3
#define CMD_GET_CONTACTS              4
#define CMD_GET_DEVICE_TIME           5
#define CMD_SET_DEVICE_TIME           6
#define CMD_SYNC_NEXT_MESSAGE         10
#define CMD_GET_BATT_AND_STORAGE      20
#define CMD_DEVICE_QUERY              22
#define CMD_GET_CHANNEL               31
#define CMD_SET_CHANNEL               32
#define CMD_SEND_TRACE_PATH           36
#define CMD_GET_STATS                 56
#define CMD_RUN_CLI_COMMAND           66

// stats sub-types for CMD_GET_STATS
#define STATS_TYPE_CORE               0
#define STATS_TYPE_RADIO              1
#define STATS_TYPE_PACKETS            2

// node -> host replies
#define RESP_CODE_OK                  0
#define RESP_CODE_ERR                 1
#define RESP_CODE_CONTACTS_START      2
#define RESP_CODE_CONTACT             3
#define RESP_CODE_END_OF_CONTACTS     4
#define RESP_CODE_SELF_INFO           5
#define RESP_CODE_SENT                6
#define RESP_CODE_CURR_TIME           9
#define RESP_CODE_NO_MORE_MESSAGES    10
#define RESP_CODE_BATT_AND_STORAGE    12
#define RESP_CODE_DEVICE_INFO         13
#define RESP_CODE_CONTACT_MSG_RECV_V3 16
#define RESP_CODE_CHANNEL_MSG_RECV_V3 17
#define RESP_CODE_CHANNEL_INFO        18
#define RESP_CODE_STATS               24
#define RESP_CODE_CLI_REPLY           29

// node -> host pushes
#define PUSH_CODE_MSG_WAITING         0x83
#define PUSH_CODE_TRACE_DATA          0x89
#define PUSH_CODE_NEW_ADVERT          0x8A

#define ERR_CODE_UNSUPPORTED_CMD      1
#define ERR_CODE_NOT_FOUND            2
#define ERR_CODE_TABLE_FULL           3
#define ERR_CODE_BAD_STATE            4
#define ERR_CODE_FILE_IO_ERROR        5
#define ERR_CODE_ILLEGAL_ARG          6

// HostLink protocol code reported in DEVICE_INFO
#define HOSTLINK_FIRMWARE_VER_CODE    14
