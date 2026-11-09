// Package notify sends operational alerts to an ntfy topic (https://ntfy.sh).
//
// Alerts are best-effort: a failed notification is logged and never affects
// the sweep, because losing an hour of capture to a broken alert channel would
// be worse than the missed alert.
package notify

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// Notifier posts to one ntfy topic URL, e.g. https://ntfy.sh/my-secret-topic.
// A zero Notifier (empty URL) is a no-op, so alerts are opt-in per deployment.
type Notifier struct {
	URL    string
	Token  string // optional bearer token for protected topics
	Client *http.Client
}

// New returns a Notifier; an empty url disables it.
func New(url, token string) *Notifier {
	return &Notifier{URL: url, Token: token, Client: &http.Client{Timeout: 10 * time.Second}}
}

// Enabled reports whether alerts will actually be sent.
func (n *Notifier) Enabled() bool { return n != nil && n.URL != "" }

// Send posts one alert. priority follows ntfy: "default", "high", "urgent".
func (n *Notifier) Send(ctx context.Context, title, body, priority string, tags ...string) error {
	if !n.Enabled() {
		return nil
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, n.URL, strings.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Title", title)
	if priority != "" {
		req.Header.Set("Priority", priority)
	}
	if len(tags) > 0 {
		req.Header.Set("Tags", strings.Join(tags, ","))
	}
	if n.Token != "" {
		req.Header.Set("Authorization", "Bearer "+n.Token)
	}
	resp, err := n.Client.Do(req)
	if err != nil {
		return fmt.Errorf("ntfy: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("ntfy: status %d", resp.StatusCode)
	}
	return nil
}
