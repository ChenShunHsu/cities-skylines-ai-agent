namespace SkylinesAgentBridge
{
    public sealed class CommandResult
    {
        public bool Ok;
        public string Json;
        public string Error;

        public static CommandResult FromJson(string json)
        {
            CommandResult result = new CommandResult();
            result.Ok = true;
            result.Json = json;
            return result;
        }

        public static CommandResult Fail(string error)
        {
            CommandResult result = new CommandResult();
            result.Ok = false;
            result.Error = error;
            result.Json = "{\"ok\":false,\"error\":\"" + JsonUtil.Escape(error) + "\"}";
            return result;
        }

        public static CommandResult FailWithRecovery(
            string error,
            bool localRollbackAttempted,
            bool localRollbackSucceeded,
            string entityType,
            int entityId)
        {
            CommandResult result = new CommandResult();
            result.Ok = false;
            result.Error = error;
            result.Json = "{\"ok\":false,\"error\":\"" + JsonUtil.Escape(error) + "\"" +
                ",\"recovery\":{\"localRollbackAttempted\":" + JsonUtil.Bool(localRollbackAttempted) +
                ",\"localRollbackSucceeded\":" + JsonUtil.Bool(localRollbackSucceeded) +
                ",\"checkpointRestoreRequired\":" + JsonUtil.Bool(localRollbackAttempted && !localRollbackSucceeded) +
                ",\"entityType\":\"" + JsonUtil.Escape(entityType == null ? "" : entityType) + "\"" +
                ",\"entityId\":" + entityId + "}}";
            return result;
        }
    }
}
