using System;
using ColossalFramework;
using ColossalFramework.Threading;
using System.Reflection;

namespace SkylinesAgentBridge
{
    public static class GameThreadHelpers
    {
        public static void ReleaseBuilding(BuildingManager manager, ushort id)
        {
            string error;
            if (!TryReleaseBuilding(manager, id, out error))
            {
                throw new InvalidOperationException(error);
            }
        }

        public static bool TryReleaseBuilding(BuildingManager manager, ushort id, out string error)
        {
            error = null;
            if (manager == null || id == 0 || id >= manager.m_buildings.m_buffer.Length)
            {
                error = "Building release target is out of range: " + id + ".";
                return false;
            }

            // PlayerBuildingAI.ReleaseBuilding relocks the building's create-pass milestones
            // first; a relock that changes a referenced unlock (the last cargo terminal standing
            // relocks Train Track) dispatches a UI refresh that throws on this thread, and the
            // release stopped before its paths, citizen units and grid entry were freed. The
            // milestones are taken off the AI for the release and relocked after it.
            BuildingInfo releasedInfo = manager.m_buildings.m_buffer[id].Info;
            ManualMilestone[] passMilestones = DetachCreatePassMilestones(releasedInfo);
            bool released;
            try
            {
                released = TryReleaseBuildingCore(manager, id, out error);
            }
            finally
            {
                RestoreCreatePassMilestones(releasedInfo, passMilestones);
            }
            if (released)
            {
                ChangeCreatePassMilestones(passMilestones, false);
            }
            return released;
        }

        // The create-pass milestones (Train Station / Cargo Center / ... Created) a player
        // building passes when it is created and relocks when it is released, or null.
        internal static ManualMilestone[] DetachCreatePassMilestones(BuildingInfo info)
        {
            PlayerBuildingAI ai = info == null ? null : info.m_buildingAI as PlayerBuildingAI;
            if (ai == null ||
                (ai.m_createPassMilestone == null && ai.m_createPassMilestone2 == null && ai.m_createPassMilestone3 == null))
            {
                return null;
            }
            ManualMilestone[] saved = new ManualMilestone[]
            {
                ai.m_createPassMilestone, ai.m_createPassMilestone2, ai.m_createPassMilestone3
            };
            ai.m_createPassMilestone = null;
            ai.m_createPassMilestone2 = null;
            ai.m_createPassMilestone3 = null;
            return saved;
        }

        internal static string DescribeCreatePassMilestones(BuildingInfo info)
        {
            PlayerBuildingAI ai = info == null ? null : info.m_buildingAI as PlayerBuildingAI;
            if (ai == null)
            {
                return "[]";
            }
            UnlockManager unlocks = Singleton<UnlockManager>.instance;
            System.Text.StringBuilder listed = new System.Text.StringBuilder();
            ManualMilestone[] milestones = new ManualMilestone[]
            {
                ai.m_createPassMilestone, ai.m_createPassMilestone2, ai.m_createPassMilestone3
            };
            for (int index = 0; index < milestones.Length; index++)
            {
                if (milestones[index] == null)
                {
                    continue;
                }
                if (listed.Length > 0)
                {
                    listed.Append(",");
                }
                listed.Append("{\"name\":\"").Append(JsonUtil.Escape(milestones[index].name)).Append("\"");
                listed.Append(",\"passed\":").Append(JsonUtil.Bool(unlocks.Unlocked(milestones[index]))).Append("}");
            }
            return "[" + listed.ToString() + "]";
        }

        internal static void RestoreCreatePassMilestones(BuildingInfo info, ManualMilestone[] saved)
        {
            PlayerBuildingAI ai = info == null ? null : info.m_buildingAI as PlayerBuildingAI;
            if (ai == null || saved == null)
            {
                return;
            }
            ai.m_createPassMilestone = saved[0];
            ai.m_createPassMilestone2 = saved[1];
            ai.m_createPassMilestone3 = saved[2];
        }

        // Unlock (a building was created) or relock (one was released) the detached milestones as
        // the AI would. The milestone data changes before UnlockManager dispatches its UI refresh,
        // which throws on this thread; the refresh then runs directly. Returns whether every
        // unlocked milestone reads passed; names lists them for the response.
        internal static bool ChangeCreatePassMilestones(ManualMilestone[] saved, bool unlock)
        {
            string names;
            return ChangeCreatePassMilestones(saved, unlock, out names);
        }

        internal static bool ChangeCreatePassMilestones(ManualMilestone[] saved, bool unlock, out string names)
        {
            names = "[]";
            if (saved == null)
            {
                return true;
            }
            UnlockManager unlocks = Singleton<UnlockManager>.instance;
            System.Text.StringBuilder listed = new System.Text.StringBuilder();
            bool allPassed = true;
            bool changed = false;
            for (int index = 0; index < saved.Length; index++)
            {
                ManualMilestone milestone = saved[index];
                if (milestone == null)
                {
                    continue;
                }
                try
                {
                    if (unlock)
                    {
                        milestone.Unlock();
                    }
                    else
                    {
                        milestone.Relock();
                    }
                }
                catch (InvalidOperationException ex)
                {
                    if (!IsSameThreadException(ex))
                    {
                        throw;
                    }
                }
                changed = true;
                bool passed = unlocks.Unlocked(milestone);
                if (unlock && !passed)
                {
                    allPassed = false;
                }
                if (listed.Length > 0)
                {
                    listed.Append(",");
                }
                listed.Append("{\"name\":\"").Append(JsonUtil.Escape(milestone.name)).Append("\"");
                listed.Append(",\"passed\":").Append(JsonUtil.Bool(passed)).Append("}");
            }
            if (changed)
            {
                try
                {
                    ThreadHelper.dispatcher.Dispatch(delegate
                    {
                        Singleton<UnlockManager>.instance.MilestonesUpdated();
                    });
                }
                catch (InvalidOperationException ex)
                {
                    if (!IsSameThreadException(ex))
                    {
                        throw;
                    }
                    unlocks.MilestonesUpdated();
                }
            }
            names = "[" + listed.ToString() + "]";
            return allPassed;
        }

        private static bool TryReleaseBuildingCore(BuildingManager manager, ushort id, out string error)
        {
            error = null;
            ClearUiReference(new InstanceID { Building = id });

            try
            {
                manager.ReleaseBuilding(id);
            }
            catch (InvalidOperationException ex)
            {
                if (!IsSameThreadException(ex))
                {
                    error = ex.Message;
                    return false;
                }

                MethodInfo method = typeof(BuildingManager).GetMethod(
                    "ReleaseBuildingImplementation",
                    BindingFlags.Instance | BindingFlags.NonPublic,
                    null,
                    new Type[] { typeof(ushort), typeof(Building).MakeByRefType() },
                    null);

                if (method == null)
                {
                    error = "BuildingManager.ReleaseBuildingImplementation was not found.";
                    return false;
                }

                object[] args = new object[] { id, manager.m_buildings.m_buffer[id] };
                try
                {
                    InvokeSameThreadRelease(method, manager, args);
                    manager.m_buildings.m_buffer[id] = (Building)args[1];
                }
                catch (Exception implementationError)
                {
                    if ((manager.m_buildings.m_buffer[id].m_flags & Building.Flags.Created) != Building.Flags.None)
                    {
                        error = implementationError.Message;
                        return false;
                    }
                }
            }

            if ((manager.m_buildings.m_buffer[id].m_flags & Building.Flags.Created) != Building.Flags.None)
            {
                error = "Building release returned without removing building " + id + ".";
                return false;
            }
            return true;
        }

        public static void ReleaseSegment(NetManager manager, ushort id, bool keepNodes)
        {
            string error;
            if (!TryReleaseSegment(manager, id, keepNodes, out error))
            {
                throw new InvalidOperationException(error);
            }
        }

        public static bool TryReleaseSegment(NetManager manager, ushort id, bool keepNodes, out string error)
        {
            error = null;
            if (manager == null || id == 0 || id >= manager.m_segments.m_buffer.Length)
            {
                error = "Segment release target is out of range: " + id + ".";
                return false;
            }

            ClearUiReference(new InstanceID { NetSegment = id });

            try
            {
            MethodInfo method = typeof(NetManager).GetMethod(
                "ReleaseSegmentImplementation",
                BindingFlags.Instance | BindingFlags.NonPublic,
                null,
                new Type[] { typeof(ushort), typeof(bool) },
                null);

            if (method != null)
            {
                method.Invoke(manager, new object[] { id, keepNodes });
            }
            else
            {
                method = typeof(NetManager).GetMethod(
                    "ReleaseSegmentImplementation",
                    BindingFlags.Instance | BindingFlags.NonPublic,
                    null,
                    new Type[] { typeof(ushort), typeof(NetSegment).MakeByRefType(), typeof(bool) },
                    null);

                if (method == null)
                {
                    error = "NetManager.ReleaseSegmentImplementation was not found.";
                    return false;
                }

                object[] args = new object[] { id, manager.m_segments.m_buffer[id], keepNodes };
                method.Invoke(manager, args);
                manager.m_segments.m_buffer[id] = (NetSegment)args[1];
            }
            }
            catch (Exception ex)
            {
                Exception actual = ex is TargetInvocationException && ex.InnerException != null
                    ? ex.InnerException
                    : ex;
                if ((manager.m_segments.m_buffer[id].m_flags & NetSegment.Flags.Created) != NetSegment.Flags.None)
                {
                    error = actual.Message;
                    return false;
                }
            }

            if ((manager.m_segments.m_buffer[id].m_flags & NetSegment.Flags.Created) != NetSegment.Flags.None)
            {
                error = "Segment release returned without removing segment " + id + ".";
                return false;
            }
            return true;
        }

        public static bool TryReleaseNode(NetManager manager, ushort id, out string error)
        {
            error = null;
            if (manager == null || id == 0 || id >= manager.m_nodes.m_buffer.Length)
            {
                error = "Node release target is out of range: " + id + ".";
                return false;
            }

            ClearUiReference(new InstanceID { NetNode = id });
            if (manager.m_nodes.m_buffer[id].CountSegments() != 0)
            {
                error = "Node " + id + " is still connected to one or more segments.";
                return false;
            }

            try
            {
                manager.ReleaseNode(id);
            }
            catch (InvalidOperationException ex)
            {
                if (!IsSameThreadException(ex))
                {
                    error = ex.Message;
                    return false;
                }

                MethodInfo method = typeof(NetManager).GetMethod(
                    "ReleaseNodeImplementation",
                    BindingFlags.Instance | BindingFlags.NonPublic,
                    null,
                    new Type[] { typeof(ushort) },
                    null);
                if (method == null)
                {
                    method = typeof(NetManager).GetMethod(
                        "ReleaseNodeImplementation",
                        BindingFlags.Instance | BindingFlags.NonPublic,
                        null,
                        new Type[] { typeof(ushort), typeof(NetNode).MakeByRefType() },
                        null);
                }
                if (method == null)
                {
                    error = "NetManager.ReleaseNodeImplementation was not found.";
                    return false;
                }

                try
                {
                    if (method.GetParameters().Length == 1)
                    {
                        method.Invoke(manager, new object[] { id });
                    }
                    else
                    {
                        object[] args = new object[] { id, manager.m_nodes.m_buffer[id] };
                        method.Invoke(manager, args);
                        manager.m_nodes.m_buffer[id] = (NetNode)args[1];
                    }
                }
                catch (Exception implementationError)
                {
                    Exception actual = implementationError is TargetInvocationException &&
                        implementationError.InnerException != null
                        ? implementationError.InnerException
                        : implementationError;
                    if ((manager.m_nodes.m_buffer[id].m_flags & NetNode.Flags.Created) != NetNode.Flags.None)
                    {
                        error = actual.Message;
                        return false;
                    }
                }
            }

            if ((manager.m_nodes.m_buffer[id].m_flags & NetNode.Flags.Created) != NetNode.Flags.None)
            {
                error = "Node release returned without removing node " + id + ".";
                return false;
            }
            return true;
        }

        // InstanceManager.ReleaseInstance dispatches the UI "instance changed" callback to the main
        // thread when the target is selected or followed; the Bridge already runs there, so the
        // dispatcher throws "Already in the same thread" before anything is released. Run that
        // callback directly and clear the reference first so the native release proceeds.
        private static void ClearUiReference(InstanceID id)
        {
            InstanceManager instances = Singleton<InstanceManager>.instance;
            const BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic;
            FieldInfo selected = typeof(InstanceManager).GetField("m_selectedInstance", flags);
            FieldInfo followed = typeof(InstanceManager).GetField("m_followedInstance", flags);
            if (instances == null || selected == null || followed == null)
            {
                return;
            }

            bool isSelected = (InstanceID)selected.GetValue(instances) == id;
            bool isFollowed = (InstanceID)followed.GetValue(instances) == id;
            if (!isSelected && !isFollowed)
            {
                return;
            }

            FieldInfo changed = typeof(InstanceManager).GetField("m_instanceChanged", flags);
            InstanceManager.ChangeAction handler = changed == null
                ? null
                : changed.GetValue(instances) as InstanceManager.ChangeAction;
            if (handler != null)
            {
                handler(id, InstanceID.Empty);
            }
            if (isSelected)
            {
                selected.SetValue(instances, InstanceID.Empty);
            }
            if (isFollowed)
            {
                followed.SetValue(instances, InstanceID.Empty);
            }
        }

        private static void InvokeSameThreadRelease(MethodInfo method, object target, object[] args)
        {
            try
            {
                method.Invoke(target, args);
            }
            catch (TargetInvocationException ex)
            {
                InvalidOperationException inner = ex.InnerException as InvalidOperationException;
                if (inner == null || !IsSameThreadException(inner))
                {
                    throw;
                }
            }
        }

        private static bool IsSameThreadException(InvalidOperationException ex)
        {
            return ex.Message != null && ex.Message.IndexOf("Already in the same thread") >= 0;
        }
    }
}
