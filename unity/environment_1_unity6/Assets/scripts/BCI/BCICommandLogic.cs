using UnityEngine;

namespace BCI
{
    public static class BCICommandLogic
    {
        public static bool TryParse(string json, out BCIMessage msg)
        {
            msg = default;
            if (string.IsNullOrEmpty(json))
            {
                return false;
            }

            try
            {
                msg = JsonUtility.FromJson<BCIMessage>(json);
            }
            catch
            {
                return false;
            }

            string normalized = NormalizeToLegacyLabel(msg);
            return !string.IsNullOrEmpty(normalized);
        }

        public static bool IsAccepted(BCIMessage msg, float threshold, bool requireStableForNewProtocol)
        {
            string normalized = NormalizeToLegacyLabel(msg);
            if (!IsValidLabel(normalized))
            {
                return false;
            }

            // For new protocol frames, optionally require smoothing stability.
            bool isNewPrediction = msg.type == "prediction";
            if (isNewPrediction && requireStableForNewProtocol && !msg.stable)
            {
                return false;
            }

            return msg.confidence >= threshold;
        }

        public static string NormalizeToLegacyLabel(BCIMessage msg)
        {
            if (!string.IsNullOrEmpty(msg.label))
            {
                return msg.label.Trim().ToUpperInvariant();
            }

            switch ((msg.predicted_class ?? string.Empty).Trim())
            {
                case "mi_forward":
                    return "FORWARD";
                case "mi_backward":
                    return "BACKWARD";
                case "mi_rotate_left":
                case "left_motor_imagery":
                    return "LEFT";
                case "mi_rotate_right":
                case "right_motor_imagery":
                    return "RIGHT";
                default:
                    return string.Empty;
            }
        }

        public static bool IsValidLabel(string label)
        {
            return label == "LEFT"
                || label == "RIGHT"
                || label == "FORWARD"
                || label == "BACKWARD";
        }

        public static Vector3 ProjectHorizontal(Vector3 v)
        {
            v.y = 0f;
            float m = v.magnitude;
            if (m < 1e-5f)
            {
                return Vector3.forward;
            }
            return v / m;
        }

        /// Computes yaw delta (degrees around Y) and movement delta for one command.
        public static void ComputeMovement(
            string label,
            Vector3 headForward,
            float stepSize,
            out Vector3 deltaPosition,
            out float deltaYawDegrees)
        {
            deltaYawDegrees = 0f;
            deltaPosition = Vector3.zero;

            switch (label)
            {
                case "FORWARD":
                    deltaPosition = headForward * stepSize;
                    break;
                case "BACKWARD":
                    deltaPosition = -headForward * stepSize;
                    break;
                case "LEFT":
                    deltaYawDegrees = -90f;
                    deltaPosition = Quaternion.Euler(0f, -90f, 0f) * headForward * stepSize;
                    break;
                case "RIGHT":
                    deltaYawDegrees = 90f;
                    deltaPosition = Quaternion.Euler(0f, 90f, 0f) * headForward * stepSize;
                    break;
            }
        }
    }
}
