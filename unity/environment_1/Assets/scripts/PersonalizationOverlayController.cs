using TMPro;
using UnityEngine;

[DisallowMultipleComponent]
public class PersonalizationOverlayController : MonoBehaviour
{
    [SerializeField] private PredictionWebSocketClient predictionClient;
    [SerializeField] private CanvasGroup overlayCanvasGroup;
    [SerializeField] private TMP_Text statusText;
    [SerializeField] private string defaultMessage = "Personalising the ML model...";

    private void Awake()
    {
        if (statusText != null)
        {
            statusText.text = defaultMessage;
        }
        Show(false);
    }

    private void OnEnable()
    {
        if (predictionClient != null)
        {
            predictionClient.OnState += HandleState;
        }
    }

    private void OnDisable()
    {
        if (predictionClient != null)
        {
            predictionClient.OnState -= HandleState;
        }
    }

    private void HandleState(PredictionWebSocketClient.StateMessage msg)
    {
        if (msg == null || msg.type != "state")
        {
            return;
        }

        switch (msg.state)
        {
            case "personalizing_start":
                if (statusText != null)
                {
                    statusText.text = string.IsNullOrWhiteSpace(msg.message) ? defaultMessage : msg.message;
                }
                Show(true);
                break;
            case "personalizing_end":
            case "realtime_ready":
                Show(false);
                break;
        }
    }

    private void Show(bool visible)
    {
        if (overlayCanvasGroup == null)
        {
            gameObject.SetActive(visible);
            return;
        }

        overlayCanvasGroup.alpha = visible ? 1f : 0f;
        overlayCanvasGroup.interactable = visible;
        overlayCanvasGroup.blocksRaycasts = visible;
    }
}
