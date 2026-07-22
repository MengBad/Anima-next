"""AstrBot compatibility re-export."""
from anima.compat.astrbot import filter, hook, EventMessageType, PermissionType, PlatformAdapterType, command, event_message_type, permission_type, platform_adapter_type, regex, on_command, on_group_message, on_private_message, on_all_message

# 兼容函数形式（部分插件直接调用）
command = filter.command
command_group = filter.command_group
regex = filter.regex
on_command = filter.on_command
event_message_type = filter.event_message_type
permission_type = filter.permission_type
platform_adapter_type = filter.platform_adapter_type
on_group_message = filter.on_group_message
on_private_message = filter.on_private_message
on_all_message = filter.on_all_message
on_llm_request = filter.on_llm_request
on_llm_response = filter.on_llm_response
on_llm_tool_respond = filter.on_llm_tool_respond
on_using_llm_tool = filter.on_using_llm_tool
on_decorating_result = filter.on_decorating_result
on_message = hook.on_message
llm_tool = filter.llm_tool
