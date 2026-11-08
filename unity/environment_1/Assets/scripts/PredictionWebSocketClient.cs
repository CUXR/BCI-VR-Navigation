using System;
using System.Collections;
using System.Text;
using NativeWebSocket;
using UnityEngine;

[DisallowMultipleComponent]
public class PredictionWebSocketClient : MonoBehaviour
{
    [Header("Endpoint")]
    [SerializeField] private string host = "127.0.0.1";
    [SerializeField] private int port = 8765;
    [SerializeField] private bool autoConnectOnStart = true;

    [Header("Diagnostics")]
    [SerializeField] private bool logFrames = false;

    [Serializable]
    private class Envelope
    {
        public string type;
    }

    [Serializable]
    public class PredictionMessage
    {
        public string type;
        public string predicted_class;
        public float confidence;
        public bool stable;
        public string key_hint;
        public double timestamp;
    }

    [Serializable]
    public class StateMessage
    {
        public string type;
        public string state;
        public string participant;
        public string message;
        public float timestamp;
    }

    public event Action<PredictionMessage> OnPrediction;
    public event Action<StateMessage> OnState;
    public event Action OnConnected;
    public event Action OnDisconnected;

    private WebSocket socket;
    private bool isConnected;
    private bool quitting;

    public bool IsConnected => isConnected;

    private IEnumerator Start()
    {
        while (autoConnectOnStart && !quitting)
        {
            if (!isConnected && socket == null)
            {
                _ = Connect();
            }
            yield return new WaitForSecondsRealtime(2f);
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
        quitting = true;
        await Close();
    }

    public async System.Threading.Tasks.Task Connect()
    {
        if (socket != null)
        {
            return;
        }

        string url = $"ws://{host}:{port}";
        WebSocket connection = new WebSocket(url);
        socket = connection;
        connection.OnOpen += () =>
        {
            isConnected = true;
            Debug.Log("[PredictionWS] Connected: " + url);
            OnConnected?.Invoke();
        };
        connection.OnError += e => Debug.LogWarning("[PredictionWS] Error: " + e);
        connection.OnClose += e =>
        {
            if (socket != connection)
            {
                return;
            }
            isConnected = false;
            socket = null;
            Debug.Log("[PredictionWS] Closed");
            OnDisconnected?.Invoke();
        };
        connection.OnMessage += HandleMessage;

        try
        {
            await connection.Connect();
        }
        catch (Exception e)
        {
            Debug.LogWarning("[PredictionWS] Connect failed: " + e.Message);
            if (socket == connection)
            {
                socket = null;
                isConnected = false;
                OnDisconnected?.Invoke();
            }
        }
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
            // Scene teardown best effort.
        }
        socket = null;
        isConnected = false;
    }

    private void HandleMessage(byte[] bytes)
    {
        string json = Encoding.UTF8.GetString(bytes);
        if (logFrames)
        {
            Debug.Log("[PredictionWS] <- " + json);
        }

        Envelope envelope = JsonUtility.FromJson<Envelope>(json);
        if (envelope != null && envelope.type == "state")
        {
            StateMessage state = JsonUtility.FromJson<StateMessage>(json);
            OnState?.Invoke(state);
            return;
        }

        if (envelope != null && envelope.type == "signal_quality")
        {
            return;
        }

        PredictionMessage prediction = JsonUtility.FromJson<PredictionMessage>(json);
        if (prediction == null)
        {
            return;
        }
        OnPrediction?.Invoke(prediction);
    }
}
