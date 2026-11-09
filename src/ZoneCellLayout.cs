using System;

namespace SkylinesAgentBridge
{
    // Native validity/occupancy masks use eight bits per row, although only
    // four columns contain zoning cells. Zone type nibbles are a separate layout.
    public static class ZoneCellLayout
    {
        public static ulong Bit(int x, int z)
        {
            if (x < 0 || x >= 4 || z < 0 || z >= 8)
                throw new ArgumentOutOfRangeException("cell");
            return 1UL << (z * 8 + x);
        }

        public static int Count(ulong mask, int rows)
        {
            if (rows < 0 || rows > 8) throw new ArgumentOutOfRangeException("rows");
            int count = 0;
            for (int z = 0; z < rows; z++)
                for (int x = 0; x < 4; x++)
                    if ((mask & Bit(x, z)) != 0) count++;
            return count;
        }

        public static double[] Point(double px, double pz, double angle, double x, double z)
        {
            double dx = Math.Cos(angle) * 8.0;
            double dz = Math.Sin(angle) * 8.0;
            return new double[] { px + (x - 4) * dx + (z - 4) * dz,
                pz + (x - 4) * dz - (z - 4) * dx };
        }
    }
}
