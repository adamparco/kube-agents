// Command a2a is the topics client: read the current answer on a provisioned
// topic, or write one. It is the reader beat 3 needs before any session pod
// exists, and the thing the platform agent's a2a-topics skill shells out to.
// Its `notify` command asks the A2A gateway to post a proactive message to the
// chat backend it holds (notify.go).
//
// Credentials: in the platform-agent container, which is where this binary
// mostly runs, it authenticates through the auth callout as `agent` -- the
// operator projects a ServiceAccount token bound to the `a2a-bus` audience and
// names the principal in A2A_BUS_USER, and no password is rendered beside it.
// Hand-run at a port-forward there is no token file, so a static per-role NATS
// user in the environment is all there is; connect() below chooses by whether a
// token is present rather than by a mode flag. The subject grants it dials into
// are the real ones either way.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"math"
	"os"
	"sort"
	"strconv"
	"strings"
	"text/tabwriter"
	"time"
	"unicode/utf16"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

const usage = `a2a - a2a-jetstream topics and action proposal client

usage:
  a2a topics list                  list the provisioned topics and their retention class
  a2a topics read <topic>          print the latest entry on a topic
  a2a topics write <topic> [flags] publish one entry to a topic
  a2a notify --platform <p> [text] post to the chat home channel, or a live conversation, via the gateway
  a2a propose --file <path|->      mint a broker capability hop and propose an ActionEnvelope

<topic> is a bare name (upgrade-readiness), a scope-qualified name
(shared.blueprint, agent.platform.upgrade-readiness), or a full subject. A bare
name that matches more than one provisioned topic is an error, not a guess.

environment:
  NATS_URL            bus address (required; A2A_NATS_URL also accepted)
  A2A_BUS_USER        the principal to connect as; also selects the
                      _INBOX.<user> prefix. Rendered by the operator.
  A2A_BUS_TOKEN_FILE  a projected ServiceAccount token to authenticate with
                      (default: /var/run/secrets/a2a-bus/token, used when it
                      exists). Setting this asserts the token; a path that
                      cannot be read is an error, not a fallback.
  NATS_USER           static bus user, if there is no token. Also serves as
                      A2A_BUS_USER when that is unset.
  NATS_PASSWORD       its password
  A2A_SESSION         from.session on writes (default: the bus user)
  A2A_PROFILE         from.profile on writes, when the writer runs as a profile
`

// cliTimeout bounds one CLI operation end to end, connect included.
const cliTimeout = 30 * time.Second

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "a2a: %v\n", err)
		if errors.Is(err, errNotifyOutcomeUnknown) {
			os.Exit(notifyExitOutcomeUnknown)
		}
		if errors.Is(err, errNotifyRouteUnavailable) {
			os.Exit(notifyExitRouteUnavailable)
		}
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		fmt.Fprint(os.Stderr, usage)
		return errors.New("no command")
	}
	switch args[0] {
	case "topics":
		return runTopics(args[1:])
	case "notify":
		return runNotify(args[1:])
	case "propose":
		return runPropose(args[1:], os.Stdin, os.Stdout)
	case "mint-root":
		return runMintRoot(args[1:], os.Stdout)
	case "-h", "--help", "help":
		fmt.Print(usage)
		return nil
	default:
		fmt.Fprint(os.Stderr, usage)
		return fmt.Errorf("unknown command %q", args[0])
	}
}

func runTopics(args []string) error {
	if len(args) == 0 {
		fmt.Fprint(os.Stderr, usage)
		return errors.New("topics: no subcommand")
	}
	switch args[0] {
	case "list", "ls":
		return topicsList(args[1:])
	case "read", "get":
		return topicsRead(args[1:])
	case "write", "publish":
		return topicsWrite(args[1:])
	default:
		fmt.Fprint(os.Stderr, usage)
		return fmt.Errorf("topics: unknown subcommand %q", args[0])
	}
}

// busUser is the principal this CLI connects as. It is also the inbox prefix
// it has to pin and the default writer identity on a topic entry, which is why
// it is one function rather than three reads of the environment.
//
// In the agent pod the operator renders lib.EnvBusUser; NATS_USER is the older
// spelling and still works for a static login, so it is the fallback rather
// than a second supported name.
func busUser() string {
	if u := os.Getenv(lib.EnvBusUser); u != "" {
		return u
	}
	return os.Getenv("NATS_USER")
}

// connect dials the bus and pins _INBOX.<busUser>, which is the prefix the
// grant covers; nats.go's default inbox is not in any principal's grant, so a
// client that skips this authenticates and then times out on every reply.
//
// Token first, password second, and the choice is made by whether a token is
// there rather than by a mode flag: under `mode: next` the operator mounts a
// projected ServiceAccount token into this container and the callout resolves
// it, and off-cluster there is no such file and a static login is all there is.
// An explicitly set lib.EnvBusTokenFile is an assertion and gets no fallback —
// a caller who names a token file and is quietly logged in as something else is
// the failure this ordering exists to prevent.
func connect(ctx context.Context, name string) (*lib.Client, error) {
	url := os.Getenv("NATS_URL")
	if url == "" {
		url = os.Getenv("A2A_NATS_URL")
	}
	if url == "" {
		return nil, errors.New("NATS_URL is not set")
	}
	user := busUser()
	if user == "" {
		return nil, fmt.Errorf("no bus identity: set %s or NATS_USER", lib.EnvBusUser)
	}

	auth := lib.WithUserPassword(user, os.Getenv("NATS_PASSWORD"))
	if path := os.Getenv(lib.EnvBusTokenFile); path != "" {
		auth = lib.WithKSAToken(path, user)
	} else if _, err := os.Stat(lib.BusTokenPath); err == nil {
		auth = lib.WithKSAToken(lib.BusTokenPath, user)
	}

	// The library logs connection events at info; a CLI should say nothing
	// unless something is wrong, so only errors reach stderr.
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelError}))
	return lib.Connect(ctx, url,
		lib.WithName("a2a-cli-"+name),
		lib.WithLogger(log),
		auth,
	)
}

func cliContext() (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), cliTimeout)
}

func topicsList(args []string) error {
	fs := flag.NewFlagSet("topics list", flag.ContinueOnError)
	asJSON := fs.Bool("json", false, "print the registry as JSON")
	if err := fs.Parse(args); err != nil {
		return err
	}
	ctx, cancel := cliContext()
	defer cancel()
	c, err := connect(ctx, "topics-list")
	if err != nil {
		return err
	}
	defer c.Close()

	registry, err := c.TopicRegistry(ctx)
	if err != nil {
		return err
	}
	if *asJSON {
		return json.NewEncoder(os.Stdout).Encode(registry)
	}
	if len(registry) == 0 {
		fmt.Println("no topics provisioned")
		return nil
	}
	w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "TOPIC\tCLASS\tWRITER SCOPE\tSUBJECT")
	for _, e := range registry {
		scope := e.Scope
		if e.Agent != "" {
			scope = e.Scope + " " + e.Agent
		}
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\n", e.Topic, e.Class, scope, e.Subject)
	}
	return w.Flush()
}

// splitTopicName pulls the topic name out of the argument list before the
// flags are parsed. Go's flag package stops at the first non-flag argument, so
// `topics write upgrade-readiness --text ...` would otherwise parse zero flags
// and report three stray positionals - and the natural way to write the
// command puts the topic first.
func splitTopicName(args []string) (string, []string) {
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		return args[0], args[1:]
	}
	return "", args
}

// topicName resolves the name from either position: ahead of the flags, or
// after them.
func topicName(pre string, fs *flag.FlagSet, cmd string) (string, error) {
	switch {
	case pre != "" && fs.NArg() == 0:
		return pre, nil
	case pre == "" && fs.NArg() == 1:
		return fs.Arg(0), nil
	default:
		return "", fmt.Errorf("%s takes exactly one topic name", cmd)
	}
}

func topicsRead(args []string) error {
	name, rest := splitTopicName(args)
	fs := flag.NewFlagSet("topics read", flag.ContinueOnError)
	asJSON := fs.Bool("json", false, "print the whole envelope as JSON")
	if err := fs.Parse(rest); err != nil {
		return err
	}
	name, err := topicName(name, fs, "topics read")
	if err != nil {
		return err
	}
	ctx, cancel := cliContext()
	defer cancel()
	c, err := connect(ctx, "topics-read")
	if err != nil {
		return err
	}
	defer c.Close()

	entry, err := resolve(ctx, c, name)
	if err != nil {
		return err
	}
	env, err := c.ReadTopicLatest(ctx, entry.Stream, entry.Subject)
	if err != nil {
		if errors.Is(err, lib.ErrTopicEmpty) {
			// A provisioned topic nobody has written yet is a legal state, and
			// the caller is usually an agent about to answer a question from
			// it: say so in words it can relay, and exit non-zero so a script
			// does not mistake silence for content.
			fmt.Printf("topic %s (%s) is provisioned but has no entries yet\n", entry.Topic, entry.Subject)
			os.Exit(2)
		}
		return err
	}
	if *asJSON {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		return enc.Encode(env)
	}
	return printEntry(os.Stdout, entry, env)
}

// printEntry renders one topic entry for a reader that is usually a language
// model: the provenance line first (who wrote it, when, under what
// correlation), then the summary, then the structured state. Its shape is the
// skill's contract, so it stays stable.
func printEntry(w io.Writer, entry lib.TopicEntry, env *lib.Envelope) error {
	var a lib.Artifact
	if err := json.Unmarshal(env.Payload, &a); err != nil {
		return fmt.Errorf("malformed topic artifact on %s: %w", entry.Subject, err)
	}
	writer := env.From.Session
	if env.From.Profile != "" {
		writer += " (profile " + env.From.Profile + ")"
	}
	fmt.Fprintf(w, "topic:       %s (%s class, %s)\n", entry.Topic, entry.Class, entry.Subject)
	fmt.Fprintf(w, "written by:  %s at %s\n", writer, env.TS.Format(time.RFC3339))
	fmt.Fprintf(w, "correlation: %s\n", env.CorrelationID)
	if env.TaskID != "" {
		fmt.Fprintf(w, "written during task: %s\n", env.TaskID)
	}
	for _, p := range a.Parts {
		switch p.Kind {
		case "text":
			fmt.Fprintf(w, "\nsummary:\n%s\n", strings.TrimRight(p.Text, "\n"))
		case "data":
			var pretty json.RawMessage = p.Data
			var buf strings.Builder
			if err := indentJSON(&buf, p.Data); err == nil {
				fmt.Fprintf(w, "\ndata:\n%s\n", buf.String())
				continue
			}
			fmt.Fprintf(w, "\ndata:\n%s\n", pretty)
		case "file":
			fmt.Fprintf(w, "\nfile: %s\n", p.File.Name)
		}
	}
	return nil
}

func indentJSON(w *strings.Builder, raw json.RawMessage) error {
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		return err
	}
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		return err
	}
	return nil
}

func topicsWrite(args []string) error {
	name, rest := splitTopicName(args)
	fs := flag.NewFlagSet("topics write", flag.ContinueOnError)
	text := fs.String("text", "", "TextPart summary")
	data := fs.String("data", "", "DataPart JSON: inline, @file, or - for stdin")
	from := fs.String("from", "", "from.session (default: A2A_SESSION, else the bus user)")
	profile := fs.String("profile", os.Getenv("A2A_PROFILE"), "from.profile, when the writer runs as a profile")
	taskID := fs.String("task-id", "", "the task this write happened in the course of")
	contextID := fs.String("context-id", "", "that task's contextId")
	correlationID := fs.String("correlation-id", "", "correlationId (default: minted for this run)")
	if err := fs.Parse(rest); err != nil {
		return err
	}
	name, err := topicName(name, fs, "topics write")
	if err != nil {
		return err
	}
	if *text == "" && *data == "" {
		return errors.New("topics write needs --text, --data, or both")
	}
	// A write in the course of a task carries that task's ids, which is the
	// audit thread from a question to the standing state it changed. Half of
	// that thread is worse than none: the envelope requires both ids together.
	if (*taskID == "") != (*contextID == "") {
		return errors.New("--task-id and --context-id go together")
	}

	var payload any
	if *data != "" {
		raw, err := readData(*data)
		if err != nil {
			return err
		}
		if err := json.Unmarshal(raw, &payload); err != nil {
			return fmt.Errorf("--data is not valid JSON: %w", err)
		}
	}

	ctx, cancel := cliContext()
	defer cancel()
	c, err := connect(ctx, "topics-write")
	if err != nil {
		return err
	}
	defer c.Close()

	entry, err := resolve(ctx, c, name)
	if err != nil {
		return err
	}
	artifact, err := lib.NewTopicArtifact(entry.Topic, *text, payload)
	if err != nil {
		return err
	}
	session := *from
	if session == "" {
		session = os.Getenv("A2A_SESSION")
	}
	if session == "" {
		session = busUser()
	}
	if session == "" {
		return fmt.Errorf("no writer identity: set --from, A2A_SESSION, or %s", lib.EnvBusUser)
	}
	corr := *correlationID
	if corr == "" {
		corr = lib.NewCorrelationID()
	}
	err = c.PublishTopic(ctx, entry.Subject,
		lib.Party{Session: session, Profile: *profile},
		*taskID, *contextID, corr, artifact)
	if err != nil {
		return err
	}
	fmt.Printf("wrote %s (%s)\ncorrelation: %s\n", entry.Topic, entry.Subject, corr)
	return nil
}

func readData(spec string) ([]byte, error) {
	switch {
	case spec == "-":
		return io.ReadAll(os.Stdin)
	case strings.HasPrefix(spec, "@"):
		return os.ReadFile(strings.TrimPrefix(spec, "@"))
	default:
		return []byte(spec), nil
	}
}

func resolve(ctx context.Context, c *lib.Client, name string) (lib.TopicEntry, error) {
	registry, err := c.TopicRegistry(ctx)
	if err != nil {
		return lib.TopicEntry{}, err
	}
	return lib.ResolveTopic(registry, name)
}

type proposeMessage struct {
	CapabilityRef capability.Ref  `json:"capabilityRef"`
	TaskID        string          `json:"taskId,omitempty"`
	ActionID      string          `json:"actionId,omitempty"`
	Envelope      json.RawMessage `json:"envelope"`
}

type proposeEvent struct {
	ActionID   string `json:"actionId"`
	RecordName string `json:"recordName,omitempty"`
	Namespace  string `json:"namespace"`
	Phase      string `json:"phase"`
	Decision   string `json:"decision"`
	RiskClass  string `json:"riskClass,omitempty"`
	Reason     string `json:"reason,omitempty"`
	Message    string `json:"message,omitempty"`
	SessionID  string `json:"sessionId,omitempty"`
}

func loadBrokerEnvDefaults() {
	raw, err := os.ReadFile("/etc/kage/broker-env.json")
	if err != nil {
		return
	}
	var m map[string]string
	if err := json.Unmarshal(raw, &m); err != nil {
		return
	}
	for k, v := range m {
		if os.Getenv(k) == "" && v != "" {
			_ = os.Setenv(k, v)
		}
	}
}

func runPropose(args []string, in io.Reader, out io.Writer) error {
	loadBrokerEnvDefaults()
	fs := flag.NewFlagSet("propose", flag.ContinueOnError)
	fileFlag := fs.String("file", "-", "path to ActionEnvelope JSON file, or - for stdin")
	nsFlag := fs.String("namespace", firstNonEmpty(os.Getenv("A2A_NAMESPACE"), os.Getenv("POD_NAMESPACE"), os.Getenv("KUBEAGENTS_AGENT_NAMESPACE")), "target Kubernetes namespace")
	agentFlag := fs.String("agent", firstNonEmpty(os.Getenv("A2A_AGENT_NAME"), os.Getenv("KUBEAGENTS_AGENT_NAME"), busUser()), "target agent name")
	timeoutFlag := fs.Duration("timeout", cliTimeout, "timeout waiting for broker response")
	parentKeyFlag := fs.String("parent-key", os.Getenv("A2A_CAPABILITY_KEY"), "parent capability key in KV bucket cap")
	parentRevFlag := fs.Uint64("parent-rev", parseUint64Env("A2A_CAPABILITY_REVISION"), "parent capability revision in KV bucket cap")
	tierFlag := fs.String("tier", firstNonEmpty(os.Getenv("A2A_AUTHORITY_TIER"), string(capability.TierDeveloperTeam)), "capability authority tier")
	scopeFlag := fs.String("scope", os.Getenv("A2A_AUTHORITY_SCOPE"), "capability authority scope")
	seqFlag := fs.Int("seq", 1, "starting hop sequence number")
	taskIDFlag := fs.String("task-id", os.Getenv("A2A_TASK_ID"), "task identifier")
	if err := fs.Parse(args); err != nil {
		return err
	}

	namespace := strings.TrimSpace(*nsFlag)
	if namespace == "" {
		return errors.New("propose: --namespace (or A2A_NAMESPACE / POD_NAMESPACE) is required")
	}
	agent := strings.TrimSpace(*agentFlag)
	if agent == "" {
		return errors.New("propose: --agent (or A2A_AGENT_NAME) is required")
	}

	var rawEnv []byte
	var err error
	if *fileFlag == "-" || *fileFlag == "" {
		rawEnv, err = io.ReadAll(in)
	} else {
		rawEnv, err = os.ReadFile(strings.TrimPrefix(*fileFlag, "@"))
	}
	if err != nil {
		return fmt.Errorf("propose: reading envelope: %w", err)
	}

	canonEnv, err := jcsCanonicalize(rawEnv)
	if err != nil {
		return fmt.Errorf("propose: canonicalizing ActionEnvelope JSON: %w", err)
	}
	sum := sha256.Sum256(canonEnv)
	envSHA := hex.EncodeToString(sum[:])

	ctx, cancel := context.WithTimeout(context.Background(), *timeoutFlag)
	defer cancel()

	c, err := connect(ctx, "propose")
	if err != nil {
		return err
	}
	defer c.Close()

	writer := firstNonEmpty(os.Getenv("A2A_SESSION"), busUser())
	if writer == "" {
		return fmt.Errorf("propose: no writer identity: set A2A_SESSION or %s", lib.EnvBusUser)
	}

	scope := strings.TrimSpace(*scopeFlag)
	if scope == "" {
		scope = string(capability.NamespaceScope(namespace))
	}

	parentKey := strings.TrimSpace(*parentKeyFlag)
	parentRev := *parentRevFlag
	if parentKey == "" || parentRev == 0 {
		return errors.New("propose: --parent-key / A2A_CAPABILITY_KEY and --parent-rev / A2A_CAPABILITY_REVISION are required to mint a broker capability hop")
	}

	minter := capability.NewMinter(c.JetStream())
	entry := capability.Entry{
		Tier:           capability.Tier(strings.TrimSpace(*tierFlag)),
		Scope:          capability.Scope(scope),
		Delegate:       "broker",
		EnvelopeSHA256: envSHA,
	}
	parentRef := capability.Ref{Key: parentKey, Revision: parentRev}

	var childRef capability.Ref
	var mintErr error
	startSeq := *seqFlag
	if startSeq < 1 {
		startSeq = 1
	}
	for seq := startSeq; seq < startSeq+32; seq++ {
		childRef, mintErr = minter.MintHop(ctx, writer, seq, parentRef, entry)
		if mintErr == nil {
			break
		}
		if !strings.Contains(strings.ToLower(mintErr.Error()), "key exists") &&
			!strings.Contains(strings.ToLower(mintErr.Error()), "wrong last sequence") {
			return fmt.Errorf("propose: minting broker capability hop: %w", mintErr)
		}
	}
	if mintErr != nil {
		tsSeq := int(time.Now().UnixNano()%1000000000) + 1
		childRef, mintErr = minter.MintHop(ctx, writer, tsSeq, parentRef, entry)
	}
	if mintErr != nil {
		return fmt.Errorf("propose: minting broker capability hop: %w", mintErr)
	}

	eventsSubj := "a2a.broker.events." + namespace + ".>"
	var pullSub *nats.Subscription
	if js, jsErr := c.Conn().JetStream(); jsErr == nil {
		pullSub, _ = js.PullSubscribe("a2a.broker.events."+namespace+".*", "", nats.BindStream("BROKER"), nats.DeliverNew(), nats.InactiveThreshold(30*time.Second))
	}

	var sub *nats.Subscription
	if pullSub == nil {
		sub, err = c.Conn().SubscribeSync(eventsSubj)
		if err != nil {
			return fmt.Errorf("propose: subscribing to %s: %w", eventsSubj, err)
		}
		defer func() { _ = sub.Unsubscribe() }()
		if err := c.Conn().Flush(); err != nil {
			return fmt.Errorf("propose: flushing event subscription: %w", err)
		}
	}

	msgBytes, err := json.Marshal(proposeMessage{
		CapabilityRef: childRef,
		TaskID:        strings.TrimSpace(*taskIDFlag),
		Envelope:      json.RawMessage(canonEnv),
	})
	if err != nil {
		return fmt.Errorf("propose: marshaling proposal: %w", err)
	}

	proposeSubj := "a2a.broker.propose." + namespace + "." + agent
	if err := c.Conn().Publish(proposeSubj, msgBytes); err != nil {
		return fmt.Errorf("propose: publishing to %s: %w", proposeSubj, err)
	}

	var evMsg *nats.Msg
	if pullSub != nil {
		for ctx.Err() == nil {
			msgs, fErr := pullSub.Fetch(1, nats.MaxWait(500*time.Millisecond))
			if fErr == nil && len(msgs) > 0 {
				evMsg = msgs[0]
				break
			}
		}
		if evMsg == nil {
			return fmt.Errorf("propose: waiting for broker event on %s: %w", eventsSubj, ctx.Err())
		}
	} else {
		evMsg, err = sub.NextMsgWithContext(ctx)
		if err != nil {
			return fmt.Errorf("propose: waiting for broker event on %s: %w", eventsSubj, err)
		}
	}

	_, _ = fmt.Fprintln(out, strings.TrimSpace(string(evMsg.Data)))

	var ev proposeEvent
	if err := json.Unmarshal(evMsg.Data, &ev); err == nil {
		if ev.Decision == "refused" || ev.Phase == "Rejected" {
			return fmt.Errorf("proposal refused (%s): %s", ev.Reason, ev.Message)
		}
	}
	return nil
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if s := strings.TrimSpace(v); s != "" {
			return s
		}
	}
	return ""
}

func parseUint64Env(key string) uint64 {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return 0
	}
	n, _ := strconv.ParseUint(v, 10, 64)
	return n
}

// jcsCanonicalize implements RFC 8785 JSON Canonicalization Scheme (JCS).
func jcsCanonicalize(raw []byte) ([]byte, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	if dec.More() {
		return nil, errors.New("trailing data after JSON value")
	}
	var buf bytes.Buffer
	if err := writeJCS(&buf, v); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func writeJCS(buf *bytes.Buffer, v any) error {
	switch val := v.(type) {
	case nil:
		buf.WriteString("null")
	case bool:
		if val {
			buf.WriteString("true")
		} else {
			buf.WriteString("false")
		}
	case json.Number:
		s, err := formatJCSNumber(val)
		if err != nil {
			return err
		}
		buf.WriteString(s)
	case string:
		writeJCSString(buf, val)
	case []any:
		buf.WriteByte('[')
		for i, elem := range val {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := writeJCS(buf, elem); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(val))
		for k := range val {
			keys = append(keys, k)
		}
		sort.Slice(keys, func(i, j int) bool {
			return compareUTF16Units(keys[i], keys[j]) < 0
		})
		buf.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				buf.WriteByte(',')
			}
			writeJCSString(buf, k)
			buf.WriteByte(':')
			if err := writeJCS(buf, val[k]); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
	default:
		return fmt.Errorf("unsupported JSON value type %T", v)
	}
	return nil
}

func formatJCSNumber(n json.Number) (string, error) {
	f, err := strconv.ParseFloat(string(n), 64)
	if err != nil {
		return "", err
	}
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return "", errors.New("non-finite JSON number")
	}
	if f == 0 {
		return "0", nil
	}
	abs := math.Abs(f)
	if abs >= 1e-6 && abs < 1e21 {
		return strconv.FormatFloat(f, 'f', -1, 64), nil
	}
	s := strconv.FormatFloat(f, 'e', -1, 64)
	if idx := strings.IndexByte(s, 'e'); idx >= 0 {
		exp := s[idx+1:]
		sign := ""
		if strings.HasPrefix(exp, "+") || strings.HasPrefix(exp, "-") {
			sign = exp[:1]
			exp = exp[1:]
		}
		exp = strings.TrimLeft(exp, "0")
		if exp == "" {
			exp = "0"
		}
		s = s[:idx+1] + sign + exp
	}
	return s, nil
}

func writeJCSString(buf *bytes.Buffer, s string) {
	buf.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			buf.WriteString(`\"`)
		case '\\':
			buf.WriteString(`\\`)
		case '\b':
			buf.WriteString(`\b`)
		case '\f':
			buf.WriteString(`\f`)
		case '\n':
			buf.WriteString(`\n`)
		case '\r':
			buf.WriteString(`\r`)
		case '\t':
			buf.WriteString(`\t`)
		default:
			if r < 0x20 {
				fmt.Fprintf(buf, `\u%04x`, r)
			} else {
				buf.WriteRune(r)
			}
		}
	}
	buf.WriteByte('"')
}

func compareUTF16Units(a, b string) int {
	ua := utf16.Encode([]rune(a))
	ub := utf16.Encode([]rune(b))
	for i := 0; i < len(ua) && i < len(ub); i++ {
		if ua[i] != ub[i] {
			if ua[i] < ub[i] {
				return -1
			}
			return 1
		}
	}
	if len(ua) < len(ub) {
		return -1
	}
	if len(ua) > len(ub) {
		return 1
	}
	return 0
}

func runMintRoot(args []string, stdout io.Writer) error {
	fs := flag.NewFlagSet("a2a mint-root", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	var id, tier, scope, delegate, requester string
	fs.StringVar(&id, "id", "platform-chat", "root request id")
	fs.StringVar(&tier, "tier", "platform", "authority tier")
	fs.StringVar(&scope, "scope", "namespace/kubeagents-system", "authority scope")
	fs.StringVar(&delegate, "delegate", "platform", "delegate principal")
	fs.StringVar(&requester, "requester", "agent:platform-agent", "canonical requester")
	if err := fs.Parse(args); err != nil {
		return fmt.Errorf("mint-root: %w", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), cliTimeout)
	defer cancel()

	c, err := connect(ctx, "a2a-mint-root")
	if err != nil {
		return fmt.Errorf("mint-root: %w", err)
	}
	defer c.Close()

	minter := capability.NewMinter(c.JetStream())
	ref, err := minter.Mint(ctx, id, capability.Entry{
		Tier:      capability.Tier(tier),
		Scope:     capability.Scope(scope),
		Delegate:  delegate,
		Requester: requester,
	})
	if err != nil {
		return fmt.Errorf("mint-root: %w", err)
	}
	b, _ := json.Marshal(ref)
	fmt.Fprintln(stdout, string(b))
	return nil
}

