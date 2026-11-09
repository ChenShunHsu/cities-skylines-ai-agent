using ColossalFramework;
using System;
using System.IO;
using System.Text;
using UnityEngine;

namespace SkylinesAgentBridge
{
    public static class SaveCommands
    {
        private static string restorePath = "";
        private static string restoreState = "idle";
        private static string restoreError = "";
        private static DateTime restoreRequestedUtc;

        // The last save SavePanel accepted, read by /state/save-status from the API thread.
        private static readonly object lastRequestGate = new object();
        private static string lastRequestedName = "";
        private static string lastRequestedPath = "";
        private static DateTime lastRequestedUtc;

        public static CommandResult Save(string body)
        {
            string name = JsonUtil.GetString(body, "name", "");
            if (name == null || name.Trim().Length == 0)
            {
                name = "AgentAutoSave";
            }

            name = SanitizeName(name);
            if (name.Length == 0)
            {
                name = "AgentAutoSave";
            }

            if (string.Equals(name, "AutoSave", StringComparison.OrdinalIgnoreCase))
            {
                return CommandResult.Fail("Refusing to save as AutoSave.crp. Use a unique named save to avoid CS1 autosave file sharing violations.");
            }

            EnsureAutoSaveDisabled();

            if (SavePanel.isSaving)
            {
                return CommandResult.Fail("A save is already in progress.");
            }

            SavePanel panel = FindSavePanel();
            if (panel == null)
            {
                return CommandResult.Fail("SavePanel was not found. The in-game UI may not be loaded yet.");
            }

            bool accepted = panel.SaveGame(name);
            string path = GetLocalSavePath(name);
            if (!accepted)
            {
                return CommandResult.Fail("SavePanel rejected the save request.");
            }

            lock (lastRequestGate)
            {
                lastRequestedName = name;
                lastRequestedPath = path;
                lastRequestedUtc = DateTime.UtcNow;
            }

            Debug.Log("[SkylinesAgentBridge] Requested package save: " + name + " -> " + path);
            return CommandResult.FromJson("{\"ok\":true,\"saveName\":\"" + JsonUtil.Escape(name) +
                "\",\"path\":\"" + JsonUtil.Escape(path) +
                "\",\"isSaving\":" + JsonUtil.Bool(SavePanel.isSaving) +
                ",\"message\":\"Save requested through SavePanel. Poll /state/saves until the file exists.\"}");
        }

        public static CommandResult CreateCheckpoint(string body)
        {
            string label = JsonUtil.GetString(body, "name", "BeforeMutation");
            label = SanitizeName(label);
            if (label.Length == 0) label = "BeforeMutation";
            string name = "AgentCheckpoint-" + DateTime.UtcNow.ToString("yyyyMMdd-HHmmss") + "-" + label;
            return Save("{\"name\":\"" + JsonUtil.Escape(name) + "\"}");
        }

        public static CommandResult RestoreCheckpoint(string body)
        {
            string requested = JsonUtil.GetString(body, "path", JsonUtil.GetString(body, "name", "")).Trim();
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            bool confirm = JsonUtil.GetBool(body, "confirm", false);
            if (requested.Length == 0) return CommandResult.Fail("path or name is required.");
            string saveDirectory = Path.GetFullPath(GetLocalSaveDirectory());
            string path = requested;
            if (!Path.IsPathRooted(path)) path = Path.Combine(saveDirectory, path);
            if (!path.EndsWith(".crp", StringComparison.OrdinalIgnoreCase)) path += ".crp";
            path = Path.GetFullPath(path);
            string parent = Path.GetDirectoryName(path);
            if (!string.Equals(parent, saveDirectory, StringComparison.OrdinalIgnoreCase))
            {
                return CommandResult.Fail("Checkpoint must be inside the local Cities: Skylines Saves directory.");
            }
            if (!File.Exists(path)) return CommandResult.Fail("Checkpoint file was not found: " + path);
            if (!dryRun && !confirm) return CommandResult.Fail("Set confirm=true after creating or verifying a pre-restore checkpoint.");
            if (dryRun)
            {
                FileInfo file = new FileInfo(path);
                return CommandResult.FromJson("{\"ok\":true,\"dryRun\":true,\"path\":\"" + JsonUtil.Escape(path) +
                    "\",\"length\":" + file.Length +
                    ",\"lastWriteTimeUtc\":\"" + JsonUtil.Escape(file.LastWriteTimeUtc.ToString("s")) + "\"" +
                    ",\"inProcessRestoreSupported\":false" +
                    ",\"requiredRecovery\":\"fully restart Cities: Skylines and load the verified checkpoint from the normal game load flow\"}");
            }

            restorePath = path;
            restoreState = "blocked";
            restoreError = "In-process checkpoint loading is disabled because CS1 can reject or partially load package data and report an unsupported file-format version.";
            restoreRequestedUtc = DateTime.UtcNow;
            return CommandResult.Fail(
                restoreError + " No load was attempted. Fully restart Cities: Skylines and select the verified checkpoint through the normal load-game flow.");
        }

        public static CommandResult BuildRestoreStatusJson()
        {
            return CommandResult.FromJson("{\"ok\":true,\"state\":\"" + JsonUtil.Escape(restoreState) +
                "\",\"path\":\"" + JsonUtil.Escape(restorePath) +
                "\",\"requestedUtc\":\"" + (restoreRequestedUtc == default(DateTime) ? "" : restoreRequestedUtc.ToString("s")) +
                "\",\"error\":\"" + JsonUtil.Escape(restoreError) + "\"}");
        }

        public static void MarkLevelLoaded()
        {
            if (restoreState == "loading") restoreState = "loaded";
        }

        public static CommandResult ListSaves()
        {
            // One builder for the whole listing: appending each save to a string copied the
            // listing so far every time (quadratic; live, ~1150 saves made a 297 KB answer that
            // took 0.3 s on the game thread, and 3 s while a save was being written).
            string dir = GetLocalSaveDirectory();
            StringBuilder json = new StringBuilder(4096);
            json.Append("{\"ok\":true,\"directory\":\"").Append(JsonUtil.Escape(dir))
                .Append("\",\"isSaving\":").Append(JsonUtil.Bool(SavePanel.isSaving)).Append(",\"saves\":[");
            bool first = true;

            if (Directory.Exists(dir))
            {
                FileInfo[] files = new DirectoryInfo(dir).GetFiles("*.crp");
                Array.Sort(files, delegate(FileInfo a, FileInfo b)
                {
                    return b.LastWriteTimeUtc.CompareTo(a.LastWriteTimeUtc);
                });

                for (int i = 0; i < files.Length; i++)
                {
                    FileInfo file = files[i];
                    if (!first)
                    {
                        json.Append(",");
                    }

                    json.Append("{\"name\":\"").Append(JsonUtil.Escape(Path.GetFileNameWithoutExtension(file.Name))).Append("\"")
                        .Append(",\"path\":\"").Append(JsonUtil.Escape(file.FullName)).Append("\"")
                        .Append(",\"lastWriteTimeUtc\":\"").Append(JsonUtil.Escape(file.LastWriteTimeUtc.ToString("s"))).Append("\"")
                        .Append(",\"length\":").Append(file.Length.ToString()).Append("}");
                    first = false;
                }
            }

            json.Append("]}");
            return CommandResult.FromJson(json.ToString());
        }

        // Whether a save is being written, the last save requested, and (given a name) that one
        // save's file. Answered on the API thread, not the game thread: SavePanel.isSaving is a
        // volatile static and the rest is one file lookup, so a checkpoint wait no longer queues a
        // full listing of every save behind the save it waits for (live: /state/saves polled each
        // second took 3 s per answer during a save, and grew with the number of saves).
        public static CommandResult BuildSaveStatusJson(string requestedName)
        {
            string dir = GetLocalSaveDirectory();
            StringBuilder json = new StringBuilder(512);
            json.Append("{\"ok\":true,\"isSaving\":").Append(JsonUtil.Bool(SavePanel.isSaving))
                .Append(",\"directory\":\"").Append(JsonUtil.Escape(dir)).Append("\"");

            string name, path;
            DateTime requestedUtc;
            lock (lastRequestGate)
            {
                name = lastRequestedName;
                path = lastRequestedPath;
                requestedUtc = lastRequestedUtc;
            }
            json.Append(",\"lastRequested\":");
            if (name.Length == 0)
            {
                json.Append("null");
            }
            else
            {
                json.Append("{\"name\":\"").Append(JsonUtil.Escape(name))
                    .Append("\",\"path\":\"").Append(JsonUtil.Escape(path))
                    .Append("\",\"requestedUtc\":\"").Append(requestedUtc.ToString("s")).Append("\"}");
            }

            if (requestedName != null && requestedName.Trim().Length > 0)
            {
                string saveName = requestedName.Trim();
                if (saveName.EndsWith(".crp", StringComparison.OrdinalIgnoreCase))
                {
                    saveName = saveName.Substring(0, saveName.Length - 4);
                }
                // a bare save name only: never a path that reaches outside the save folder
                if (saveName.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 || saveName.Contains(".."))
                {
                    return CommandResult.Fail("name must be a save name, not a path.");
                }
                string savePath = Path.Combine(dir, saveName + ".crp");
                FileInfo file = new FileInfo(savePath);
                json.Append(",\"save\":{\"name\":\"").Append(JsonUtil.Escape(saveName))
                    .Append("\",\"path\":\"").Append(JsonUtil.Escape(savePath))
                    .Append("\",\"exists\":").Append(JsonUtil.Bool(file.Exists));
                if (file.Exists)
                {
                    json.Append(",\"lastWriteTimeUtc\":\"").Append(file.LastWriteTimeUtc.ToString("s"))
                        .Append("\",\"length\":").Append(file.Length.ToString());
                }
                json.Append("}");
            }

            json.Append("}");
            return CommandResult.FromJson(json.ToString());
        }

        private static SavePanel FindSavePanel()
        {
            SavePanel panel = UnityEngine.Object.FindObjectOfType(typeof(SavePanel)) as SavePanel;
            if (panel != null)
            {
                return panel;
            }

            UnityEngine.Object[] panels = Resources.FindObjectsOfTypeAll(typeof(SavePanel));
            if (panels != null && panels.Length > 0)
            {
                return panels[0] as SavePanel;
            }

            return null;
        }

        private static string GetLocalSavePath(string name)
        {
            return Path.Combine(GetLocalSaveDirectory(), name + ".crp");
        }

        private static string GetLocalSaveDirectory()
        {
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "Colossal Order\\Cities_Skylines\\Saves");
        }

        private static void EnsureAutoSaveDisabled()
        {
            SavedBool autoSave = new SavedBool(Settings.autoSave, Settings.gameSettingsFile, DefaultSettings.autoSave, true);
            if (autoSave.value)
            {
                autoSave.value = false;
            }
        }

        private static string SanitizeName(string name)
        {
            char[] chars = name.Trim().ToCharArray();
            for (int i = 0; i < chars.Length; i++)
            {
                char c = chars[i];
                if (!(char.IsLetterOrDigit(c) || c == '-' || c == '_' || c == ' '))
                {
                    chars[i] = '_';
                }
            }

            string result = new string(chars);
            if (result.Length > 64)
            {
                result = result.Substring(0, 64);
            }
            return result;
        }
    }
}
