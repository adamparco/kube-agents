/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package notify_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval/notify"
)

func TestSlackDelivererPostMessage(t *testing.T) {
	var gotAuth, gotMethod string
	var gotBody map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotMethod = r.URL.Path
		json.NewDecoder(r.Body).Decode(&gotBody)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok":true,"ts":"1234.5678"}`))
	}))
	defer srv.Close()

	d := &notify.SlackDeliverer{Token: "xoxb-test", BaseURL: srv.URL}
	ref, err := d.Deliver(context.Background(), notify.Target{Platform: notify.PlatformSlack, Channel: "C01"}, askMessage())
	if err != nil {
		t.Fatalf("Deliver: %v", err)
	}
	if ref != "1234.5678" {
		t.Errorf("ref = %q, want the ts the API returned", ref)
	}
	if gotAuth != "Bearer xoxb-test" {
		t.Errorf("Authorization header = %q", gotAuth)
	}
	if !strings.HasSuffix(gotMethod, "/chat.postMessage") {
		t.Errorf("path = %q", gotMethod)
	}
	if gotBody["channel"] != "C01" {
		t.Errorf("channel = %v", gotBody["channel"])
	}
}

func TestSlackDelivererUpdateAddressesByTS(t *testing.T) {
	var gotBody map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewDecoder(r.Body).Decode(&gotBody)
		w.Write([]byte(`{"ok":true,"ts":"1234.5678"}`))
	}))
	defer srv.Close()

	d := &notify.SlackDeliverer{Token: "xoxb-test", BaseURL: srv.URL}
	m := askMessage()
	m.Resolution = "approved"
	if err := d.Update(context.Background(), notify.Target{Platform: notify.PlatformSlack, Channel: "C01"}, "1234.5678", m); err != nil {
		t.Fatalf("Update: %v", err)
	}
	if gotBody["ts"] != "1234.5678" {
		t.Errorf("ts = %v, want the original ref", gotBody["ts"])
	}
}

func TestSlackDelivererSurfacesAPIError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"ok":false,"error":"channel_not_found"}`))
	}))
	defer srv.Close()

	d := &notify.SlackDeliverer{Token: "xoxb-test", BaseURL: srv.URL}
	_, err := d.Deliver(context.Background(), notify.Target{Platform: notify.PlatformSlack, Channel: "C01"}, askMessage())
	if err == nil || !strings.Contains(err.Error(), "channel_not_found") {
		t.Fatalf("expected an error naming the slack error code, got: %v", err)
	}
}

func TestGoogleChatDelivererPostMessage(t *testing.T) {
	var gotAuth, gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotPath = r.URL.Path
		w.Write([]byte(`{"name":"spaces/AAA/messages/BBB"}`))
	}))
	defer srv.Close()

	d := &notify.GoogleChatDeliverer{
		TokenSource: func(context.Context) (string, error) { return "gctoken", nil },
		BaseURL:     srv.URL,
	}
	ref, err := d.Deliver(context.Background(), notify.Target{Platform: notify.PlatformGoogleChat, Channel: "spaces/AAA"}, askMessage())
	if err != nil {
		t.Fatalf("Deliver: %v", err)
	}
	if ref != "spaces/AAA/messages/BBB" {
		t.Errorf("ref = %q", ref)
	}
	if gotAuth != "Bearer gctoken" {
		t.Errorf("Authorization = %q", gotAuth)
	}
	if gotPath != "/spaces/AAA/messages" {
		t.Errorf("path = %q", gotPath)
	}
}

func TestGoogleChatDelivererUpdateUsesPatchWithMask(t *testing.T) {
	var gotMethod, gotQuery string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod = r.Method
		gotQuery = r.URL.RawQuery
		w.Write([]byte(`{"name":"spaces/AAA/messages/BBB"}`))
	}))
	defer srv.Close()

	d := &notify.GoogleChatDeliverer{
		TokenSource: func(context.Context) (string, error) { return "gctoken", nil },
		BaseURL:     srv.URL,
	}
	m := askMessage()
	m.Resolution = "expired"
	if err := d.Update(context.Background(), notify.Target{Platform: notify.PlatformGoogleChat}, "spaces/AAA/messages/BBB", m); err != nil {
		t.Fatalf("Update: %v", err)
	}
	if gotMethod != http.MethodPatch {
		t.Errorf("method = %q, want PATCH", gotMethod)
	}
	if !strings.Contains(gotQuery, "updateMask") {
		t.Errorf("query = %q, want an explicit updateMask", gotQuery)
	}
}

func TestGoogleChatDelivererRequiresTokenSource(t *testing.T) {
	d := &notify.GoogleChatDeliverer{}
	if _, err := d.Deliver(context.Background(), notify.Target{}, askMessage()); err == nil {
		t.Fatal("expected an error with no TokenSource configured")
	}
}

func TestDeliverersDispatchesByPlatform(t *testing.T) {
	slackCalled, gchatCalled := false, false
	ds := notify.Deliverers{
		notify.PlatformSlack:      fakeDeliverer{onDeliver: func() { slackCalled = true }},
		notify.PlatformGoogleChat: fakeDeliverer{onDeliver: func() { gchatCalled = true }},
	}
	if _, err := ds.Deliver(context.Background(), notify.Target{Platform: notify.PlatformSlack}, askMessage()); err != nil {
		t.Fatalf("Deliver: %v", err)
	}
	if !slackCalled || gchatCalled {
		t.Errorf("slackCalled=%v gchatCalled=%v, want only slack", slackCalled, gchatCalled)
	}
}

func TestDeliverersErrorsOnUnconfiguredPlatform(t *testing.T) {
	ds := notify.Deliverers{}
	if _, err := ds.Deliver(context.Background(), notify.Target{Platform: notify.PlatformSlack}, askMessage()); err == nil {
		t.Fatal("expected an error for a platform with no deliverer configured")
	}
}

type fakeDeliverer struct {
	onDeliver func()
}

func (f fakeDeliverer) Deliver(context.Context, notify.Target, approval.Message) (string, error) {
	if f.onDeliver != nil {
		f.onDeliver()
	}
	return "ref", nil
}

func (f fakeDeliverer) Update(context.Context, notify.Target, string, approval.Message) error {
	return nil
}
