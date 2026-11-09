package notify

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestSendSetsNtfyHeaders(t *testing.T) {
	var got *http.Request
	var body string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r
		b, _ := io.ReadAll(r.Body)
		body = string(b)
	}))
	defer srv.Close()

	n := New(srv.URL, "tok")
	if err := n.Send(context.Background(), "skyjam: sweep failed", "0/18 points", "high", "warning"); err != nil {
		t.Fatal(err)
	}
	if got.Header.Get("Title") != "skyjam: sweep failed" || got.Header.Get("Priority") != "high" ||
		got.Header.Get("Tags") != "warning" || got.Header.Get("Authorization") != "Bearer tok" {
		t.Fatalf("headers: %v", got.Header)
	}
	if body != "0/18 points" {
		t.Fatalf("body %q", body)
	}
}

func TestEmptyURLIsNoop(t *testing.T) {
	if New("", "").Enabled() {
		t.Fatal("empty URL must disable alerts")
	}
	if err := New("", "").Send(context.Background(), "t", "b", ""); err != nil {
		t.Fatal(err)
	}
}

func TestServerErrorIsReported(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	}))
	defer srv.Close()
	if err := New(srv.URL, "").Send(context.Background(), "t", "b", ""); err == nil {
		t.Fatal("expected an error on 403")
	}
}
