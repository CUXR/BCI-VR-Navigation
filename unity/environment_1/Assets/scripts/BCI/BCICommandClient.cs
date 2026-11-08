using System.Collections;
using UnityEngine;
using UnityEngine.Events;
using NativeWebSocket;

namespace BCI
{
    /// Main-style command client, updated to understand final-pipeline payloads.
    ///
    /// Keeps the old movement contract:
    ///   LEFT/RIGHT/FORWARD/BACKWARD
    ///
    /// while accepting both:
    ///   - legacy label payloads
    ///   - new `predicted_class` payloads from eeg_to_meta/websocket_server.py
    public class BCICommandClient : MonoBehaviour
    {
        [Header("Connection")]
        [SerializeField] private string serverUrl = "ws://127.0.0.1:8765";
        [SerializeField] private float reconnectDelaySeconds = 2f;

        [Header("Movement")]
        [SerializeField] private Transform xrOrigin;
        [SerializeField] private Transform headTransform;
        [SerializeField] private CharacterController characterController;
        [SerializeField] private float stepSize = 0.5f;

        [Header("Filtering")]
        [Range(0f, 1f)]
        [SerializeField] private float confidenceThreshold = 0.6f;
        [SerializeField] private bool requireStableForNewProtocol = true;

        [Header("Events")]
        public UnityEvent<string> OnCommand;

        private WebSocket webSocket;
        private bool quitting;

        private async void Start()
        {
            ResolveTransforms();
            await Connect();
        }

        private void ResolveTransforms()
        {
            if (xrOrigin == null)
            {
                var origin = FindObjectOfType<Unity.XR.CoreUtils.XROrigin>();
                if (origin != null)
                {
                    xrOrigin = origin.transform;
                }
            }

            if (headTransform == null && Camera.main != null)
            {
                headTransform = Camera.main.transform;
            }

            if (characterController == null && xrOrigin != null)
            {
                characterController = xrOrigin.GetComponent<CharacterController>();
            }
        }

        private async System.Threading.Tasks.Task Connect()
        {
            webSocket = new WebSocket(serverUrl);
            webSocket.OnOpen += () => Debug.Log("[BCICommandClient] Connected to " + serverUrl);
            webSocket.OnError += err => Debug.LogWarning("[BCICommandClient] Error: " + err);
            webSocket.OnClose += code =>
            {
                Debug.Log("[BCICommandClient] Closed (code=" + code + ")");
                if (!quitting)
                {
                    StartCoroutine(ReconnectAfterDelay());
                }
            };
            webSocket.OnMessage += HandleMessageBytes;

            try
            {
                await webSocket.Connect();
            }
            catch (System.Exception e)
            {
                Debug.LogWarning("[BCICommandClient] Connect failed: " + e.Message);
                if (!quitting)
                {
                    StartCoroutine(ReconnectAfterDelay());
                }
            }
        }

        private IEnumerator ReconnectAfterDelay()
        {
            yield return new WaitForSeconds(reconnectDelaySeconds);
            if (!quitting)
            {
                _ = Connect();
            }
        }

        private void HandleMessageBytes(byte[] bytes)
        {
            string json = System.Text.Encoding.UTF8.GetString(bytes);
            if (!BCICommandLogic.TryParse(json, out BCIMessage msg))
            {
                return;
            }

            if (!BCICommandLogic.IsAccepted(msg, confidenceThreshold, requireStableForNewProtocol))
            {
                return;
            }

            string label = BCICommandLogic.NormalizeToLegacyLabel(msg);
            if (string.IsNullOrEmpty(label))
            {
                return;
            }
            ApplyCommand(label);
        }

        private void ApplyCommand(string label)
        {
            if (xrOrigin != null && headTransform != null)
            {
                Vector3 headForward = BCICommandLogic.ProjectHorizontal(headTransform.forward);
                BCICommandLogic.ComputeMovement(label, headForward, stepSize, out Vector3 dPos, out float dYaw);

                if (Mathf.Abs(dYaw) > 1e-4f)
                {
                    xrOrigin.Rotate(Vector3.up, dYaw, Space.World);
                }

                if (characterController != null)
                {
                    characterController.Move(dPos);
                }
                else
                {
                    xrOrigin.position += dPos;
                }
            }

            OnCommand?.Invoke(label);
        }

        private void Update()
        {
#if !UNITY_WEBGL || UNITY_EDITOR
            webSocket?.DispatchMessageQueue();
#endif
        }

        private async void OnApplicationQuit()
        {
            quitting = true;
            if (webSocket != null)
            {
                await webSocket.Close();
            }
        }

        private async void OnDestroy()
        {
            quitting = true;
            if (webSocket != null)
            {
                await webSocket.Close();
            }
        }
    }
}
