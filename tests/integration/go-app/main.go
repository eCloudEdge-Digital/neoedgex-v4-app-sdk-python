package main

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"time"

	"github.com/eCloudEdge-Digital/neoedgex-v4-app-sdk-go/v2/neoedgex"
	mqtt "github.com/eclipse/paho.mqtt.golang"
)

const resultsTopic = "itest/results/go"

type fieldDump struct {
	Type  string `json:"type"`
	Value string `json:"value"`
}

type resultDump struct {
	Source    string               `json:"source"`
	Timestamp string               `json:"timestamp"`
	Handle    string               `json:"handle"`
	Fields    map[string]fieldDump `json:"fields"`
}

// batteryHandler publishes the type battery on "trigger" messages and dumps
// every decoded "input1" message to the results topic. The results channel
// uses its own MQTT client because NodeEnv.Publish only reaches schema-typed
// output topics, never an arbitrary one.
type batteryHandler struct {
	results mqtt.Client
}

func (h batteryHandler) Handle(ctx neoedgex.NodeEnv) {
	logger := ctx.Logger()
	for msg := range ctx.Messages() {
		switch msg.Handle {
		case "trigger":
			round := msg.ToMap()["round"]
			logger.Info("trigger received, publishing battery round %v", round)
			data := map[string]any{
				"f_bool":     true,
				"f_int16":    32767,
				"f_int32":    -123456,
				"f_int64":    int64(math.MinInt64),
				"f_uint16":   65535,
				"f_uint32":   uint32(4294967295),
				"f_uint64":   uint64(math.MaxUint64),
				"f_float":    25.34,
				"f_double":   25.34,
				"f_string":   "hello-neoedgex",
				"f_raw":      []byte{0x01, 0x02, 0xfe, 0xff},
				"f_overflow": 70000,
				"f_round":    round,
			}
			if err := ctx.Publish("output1", data); err != nil {
				ctx.ReportError(neoedgex.CodeProcessError, err)
			}
		case "input1":
			dump := resultDump{
				Source:    msg.Source,
				Timestamp: msg.Timestamp,
				Handle:    msg.Handle,
				Fields:    map[string]fieldDump{},
			}
			for key, value := range msg.ToMap() {
				dump.Fields[key] = dumpField(value)
			}
			payload, err := json.Marshal(dump)
			if err != nil {
				logger.Error("failed to marshal result dump: %v", err)
				continue
			}
			token := h.results.Publish(resultsTopic, 1, false, payload)
			if token.Wait() && token.Error() != nil {
				logger.Error("failed to publish result dump: %v", token.Error())
			}
		}
	}
}

func dumpField(value any) fieldDump {
	switch t := value.(type) {
	case nil:
		return fieldDump{Type: "nil", Value: ""}
	case []byte:
		return fieldDump{Type: "[]uint8", Value: hex.EncodeToString(t)}
	default:
		return fieldDump{Type: fmt.Sprintf("%T", t), Value: fmt.Sprintf("%v", t)}
	}
}

func main() {
	options := mqtt.NewClientOptions().
		AddBroker("tcp://neoedgex-messenger:1883").
		SetClientID("itest-results-go").
		SetConnectRetry(true).
		SetConnectRetryInterval(time.Second).
		SetAutoReconnect(true)
	results := mqtt.NewClient(options)
	if token := results.Connect(); token.Wait() && token.Error() != nil {
		log.Fatalf("results client connect: %v", token.Error())
	}
	if err := neoedgex.New(batteryHandler{results: results}).Run(); err != nil {
		log.Fatal(err)
	}
}
