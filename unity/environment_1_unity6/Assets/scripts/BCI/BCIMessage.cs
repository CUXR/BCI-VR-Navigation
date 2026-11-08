using System;

namespace BCI
{
    /// Supports both legacy and new websocket payloads:
    ///
    /// Legacy:
    ///   {"label":"LEFT","confidence":0.87,"ts":1714000000.123}
    ///
    /// New:
    ///   {"type":"prediction","predicted_class":"mi_forward","confidence":0.73,"stable":true}
    [Serializable]
    public struct BCIMessage
    {
        // Legacy fields
        public string label;
        public double ts;

        // New prediction payload fields
        public string type;
        public string predicted_class;
        public bool stable;

        // Shared
        public float confidence;
    }
}
