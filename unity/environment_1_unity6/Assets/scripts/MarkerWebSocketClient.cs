using System;
using System.Collections.Generic;
using System.Text;
using NativeWebSocket;
using UnityEngine;

[DisallowMultipleComponent]
public class MarkerWebSocketClient : MonoBehaviour
{
    [Header("Endpoint")]
    [SerializeField] private string host = "127.0.0.1";
    [SerializeField] private int port = 8766;
    [SerializeField] private bool autoConnectOnStart = true;

    [Header("Diagnostics")]
    [SerializeField] private bool logFrames = false;

    private WebSocket socket;
    private readonly Queue<string> outbound = new Queue<string>();
    private bool isConnected;

    [Serializable]
    private class MarkerMessage
    {
        public string type = "marker";
        public string phase;
        public string direction;
        public int trial_index;
        public int run_index;
        public float ts_unity;
    }

    [Serializable]
    private class ControlMessage
    {
        public string type = "control";
        public string @event;
        public int run_index;
        public float ts_unity;
    }

    public bool IsConnected => isConnected;

    private async void Start()
    {
        if (autoConnectOnStart)
        {
            await Connect();
        }
    }

    private void Update()
    {
        if (socket != null)
        {
#if !UNITY_WEBGL || UNITY_EDITOR
            socket.DispatchMessageQueue();
#endif
        }
    }

    private async void OnDestroy()
    {
        await Close();
    }

    public async System.Threading.Tasks.Task Connect()
    {
        if (socket != null)
        {
            return;
        }

        string url = $"ws://{host}:{port}";
        socket = new WebSocket(url);
        socket.OnOpen += () =>
        {
            isConnected = true;
            Debug.Log("[MarkerWS] Connected: " + url);
            FlushQueue();
        };
        socket.OnError += e => Debug.LogWarning("[MarkerWS] Error: " + e);
        socket.OnClose += e =>
        {
            isConnected = false;
            Debug.Log("[MarkerWS] Closed");
        };
        socket.OnMessage += bytes =>
        {
            if (!logFrames)
            {
                return;
            }
            Debug.Log("[MarkerWS] <- " + Encoding.UTF8.GetString(bytes));
        };

        await socket.Connect();
    }

    public async System.Threading.Tasks.Task Close()
    {
        if (socket == null)
        {
            return;
        }

        try
        {
            await socket.Close();
        }
        catch (Exception)
        {
            // Best-effort shutdown on scene unload.
        }

        socket = null;
        isConnected = false;
    }

    public void EmitMarker(string phase, string direction, int trialIndex, int runIndex)
    {
        MarkerMessage payload = new MarkerMessage
        {
            phase = phase,
            direction = direction,
            trial_index = trialIndex,
            run_index = runIndex,
            ts_unity = Time.realtimeSinceStartup
        };
        SendJson(JsonUtility.ToJson(payload));
    }

    public void EmitControl(string controlEvent, int runIndex)
    {
        ControlMessage payload = new ControlMessage
        {
            @event = controlEvent,
            run_index = runIndex,
            ts_unity = Time.realtimeSinceStartup
        };
        SendJson(JsonUtility.ToJson(payload));
    }

    private async void SendJson(string json)
    {
        if (socket == null || !isConnected)
        {
            outbound.Enqueue(json);
            if (logFrames)
            {
                Debug.Log("[MarkerWS] queued -> " + json);
            }
            return;
        }

        try
        {
            if (logFrames)
            {
                Debug.Log("[MarkerWS] -> " + json);
            }
            await socket.SendText(json);
        }
        catch (Exception e)
        {
            Debug.LogWarning("[MarkerWS] Send failed, queueing frame: " + e.Message);
            outbound.Enqueue(json);
        }
    }

    private void FlushQueue()
    {
        while (outbound.Count > 0)
        {
            string json = outbound.Dequeue();
            SendJson(json);
        }
    }
}
