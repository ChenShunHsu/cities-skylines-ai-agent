using UnityEngine;

namespace SkylinesAgentBridge
{
    public static partial class RoadCommands
    {
        // Placement search, not relaxed acceptance: adjust only NEW endpoint
        // heights from their live terrain-based seed, then repeat every native
        // preview and post-build gate. Existing node positions are immutable.
        private const float MaximumNewEndpointTerrainFit = 0.3f;
        private const float EndpointTerrainFitStep = 0.05f;

        private static void FitNewGroundRoadEndpoints(NetManager manager, NetInfo info,
            ushort startNode, ushort endNode, bool planningStart, bool planningEnd, float heightOffset,
            bool requestedStartLift, float requestedStartLiftValue,
            bool requestedEndLift, float requestedEndLiftValue,
            ref Vector3 start, ref Vector3 end, out float startLift, out float endLift)
        {
            startLift = 0f;
            endLift = 0f;
            if (!IsGroundRoad(info) || Mathf.Abs(heightOffset) > 0.01f ||
                ((startNode != 0 || planningStart) && (endNode != 0 || planningEnd))) return;
            Vector3 seedStart = start;
            Vector3 seedEnd = end;
            if (requestedStartLift || requestedEndLift)
            {
                // Explicit lifts are bounded construction intent produced by a
                // complete-route preview. They let a preceding segment create
                // the exact shared-node height required by its successor.
                // Existing and read-only planning nodes are validated by the
                // caller and are never moved here.
                if (requestedStartLift)
                {
                    start.y += requestedStartLiftValue;
                    startLift = requestedStartLiftValue;
                }
                if (requestedEndLift)
                {
                    end.y += requestedEndLiftValue;
                    endLift = requestedEndLiftValue;
                }
                return;
            }
            float flatLength = new Vector2(end.x - start.x, end.z - start.z).magnitude;
            bool originalTransition = IsOriginalNodeTransition(manager, startNode, endNode, flatLength);
            float transitionMinimumClearance = TransitionMinimumClearance(manager, startNode, endNode);
            for (int step = 0; step <= 6; step++)
            {
                float lift = Mathf.Min(step * EndpointTerrainFitStep, MaximumNewEndpointTerrainFit);
                Vector3 candidateStart = seedStart;
                Vector3 candidateEnd = seedEnd;
                if (startNode == 0 && !planningStart) candidateStart.y += lift;
                if (endNode == 0 && !planningEnd) candidateEnd.y += lift;
                Vector3 direction = (candidateEnd - candidateStart).normalized;
                Vector3 endDirection = ResolveConnectionDirection(manager, endNode, info, direction, false);
                int count;
                float deviation;
                float grade;
                float clearance;
                float settledClearance;
                float settledDeviation;
                bool safe = ValidateRoadTerrainProfile(info, candidateStart, candidateEnd,
                    direction, -endDirection, startNode != 0 || planningStart, endNode != 0 || planningEnd,
                    SettledEndKeepsSlope(manager, startNode, planningStart, candidateEnd - candidateStart),
                    SettledEndKeepsSlope(manager, endNode, planningEnd, candidateStart - candidateEnd),
                    originalTransition, transitionMinimumClearance, heightOffset, flatLength, out count,
                    out deviation, out grade, out clearance, out settledClearance, out settledDeviation);
                if (safe)
                {
                    start = candidateStart;
                    end = candidateEnd;
                    startLift = startNode == 0 && !planningStart ? lift : 0f;
                    endLift = endNode == 0 && !planningEnd ? lift : 0f;
                    return;
                }
            }
            // Retain the original placement if no bounded lift passes every
            // terrain gate. The caller still reports the failed preview and
            // repeats terrain, topology, native and settled QA before mutation.
        }
    }
}
