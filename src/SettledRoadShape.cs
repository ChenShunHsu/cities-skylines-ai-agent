using System;

namespace SkylinesAgentBridge
{
    // Settled ground roads keep their slope only through a straight Middle
    // node. Roads flatten segment directions at End, Bend and Junction nodes
    // (m_flatJunctions), so the settled curve leaves those nodes level and a
    // segment climbing a rise sags below the straight slope near its low end.
    public static class SettledRoadShape
    {
        // CS1 marks a two-segment node Middle only when the segments are almost
        // exactly opposite; anything else becomes a Bend.
        public const float MiddleNodeDirectionDot = -0.999f;

        // Live evidence: on a steady 3.6% slope the settled native curve matched
        // y = a + rise * (3t^2 - 2t^3) to within 0.011m at every sample, and its
        // deepest point sat 0.096 * rise below the chord, near t = 0.21.
        public const float FlatEndSagPerMetreOfRise = 0.096f;

        // existingSegments counts the other segments already at the node;
        // existingDirection points from the node into its existing segment and
        // newDirection from the node into the new segment.
        public static bool KeepsSlope(int existingSegments,
            float existingDirectionX, float existingDirectionZ,
            float newDirectionX, float newDirectionZ)
        {
            if (existingSegments != 1)
            {
                return false;
            }
            float existingLength = (float)Math.Sqrt(
                existingDirectionX * existingDirectionX + existingDirectionZ * existingDirectionZ);
            float newLength = (float)Math.Sqrt(newDirectionX * newDirectionX + newDirectionZ * newDirectionZ);
            if (existingLength < 0.001f || newLength < 0.001f)
            {
                return false;
            }
            float dot = (existingDirectionX * newDirectionX + existingDirectionZ * newDirectionZ) /
                (existingLength * newLength);
            return dot < MiddleNodeDirectionDot;
        }
    }
}
