package main

import (
	"context"
	"encoding/json"
	"net/http"
	"sync"
	"time"

	"github.com/Azure/azure-sdk-for-go/sdk/resourcemanager/resourcegraph/armresourcegraph"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/webdevops/go-common/prometheus/kusto"
	"github.com/webdevops/go-common/utils/to"
	"go.uber.org/zap"
)

const (
	ResourceGraphQueryOptionsTop = 1000
)

func handleProbeRequest(w http.ResponseWriter, r *http.Request) {
	registry := prometheus.NewRegistry()
	requestTime := time.Now()

	params := r.URL.Query()
	moduleName := params.Get("module")
	cacheKey := "cache:" + moduleName

	probeLogger := logger.With(zap.String("module", moduleName))

	cacheTime := 0 * time.Second
	cacheTimeDurationStr := params.Get("cache")
	if cacheTimeDurationStr != "" {
		if v, err := time.ParseDuration(cacheTimeDurationStr); err == nil {
			cacheTime = v
		} else {
			probeLogger.Errorln(err.Error())
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
	}

	ctx := context.Background()

	defaultSubscriptions := []string{}
	if subscriptionList, err := AzureClient.ListCachedSubscriptionsWithFilter(ctx, Opts.Azure.Subscription...); err == nil {
		for _, subscription := range subscriptionList {
			defaultSubscriptions = append(defaultSubscriptions, to.String(subscription.SubscriptionID))
		}
	} else {
		probeLogger.Panic(err)
	}

	// Create and authorize a ResourceGraph client
	resourceGraphClient, err := armresourcegraph.NewClient(AzureClient.GetCred(), AzureClient.NewArmClientOptions())
	if err != nil {
		probeLogger.Errorln(err.Error())
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	metricList := kusto.MetricList{}
	metricList.Init()

	// Cache handling
	executeQuery := true
	if cacheTime.Seconds() > 0 {
		if v, ok := metricCache.Get(cacheKey); ok {
			if cacheData, ok := v.([]byte); ok {
				if err := json.Unmarshal(cacheData, &metricList); err == nil {
					probeLogger.Debug("fetched from cache")
					w.Header().Add("X-metrics-cached", "true")
					executeQuery = false
				} else {
					probeLogger.Debug("unable to parse cache data")
				}
			}
		}
	}

	// Run queries if not cached
	if executeQuery {
		w.Header().Add("X-metrics-cached", "false")

		// Use a wait group and mutex to handle parallel queries
		var wg sync.WaitGroup
		var mu sync.Mutex // Protects shared resources (e.g., metricList)
		for _, queryConfig := range Config.Queries {
			if queryConfig.Module != moduleName {
				continue
			}

			wg.Add(1) // Increment the wait group counter for each query

			go func(queryConfig QueryConfig) { // Run each query in a goroutine
				defer wg.Done() // Mark the goroutine as done when the function exits
				startTime := time.Now()

				contextLogger := probeLogger.With(zap.String("metric", queryConfig.Metric))
				contextLogger.Debug("starting query")

				querySubscriptions := []*string{}
				if queryConfig.Subscriptions != nil {
					for _, val := range *queryConfig.Subscriptions {
						subscriptionID := val
						querySubscriptions = append(querySubscriptions, &subscriptionID)
					}
				} else {
					for _, val := range defaultSubscriptions {
						subscriptionID := val
						querySubscriptions = append(querySubscriptions, &subscriptionID)
					}
				}

				requestQueryTop := int32(ResourceGraphQueryOptionsTop)
				requestQuerySkip := int32(0)
				resultFormat := armresourcegraph.ResultFormatObjectArray
				RequestOptions := armresourcegraph.QueryRequestOptions{
					ResultFormat: &resultFormat,
					Top:          &requestQueryTop,
					Skip:         &requestQuerySkip,
				}

				query := queryConfig.Query
				resultTotalRecords := int32(0)

				// Loop for paginated results
				for {
					Request := armresourcegraph.QueryRequest{
						Subscriptions: querySubscriptions,
						Query:         &query,
						Options:       &RequestOptions,
					}

					prometheusQueryRequests.With(prometheus.Labels{"module": moduleName, "metric": queryConfig.Metric}).Inc()

					results, queryErr := resourceGraphClient.Resources(ctx, Request, nil)
					if results.TotalRecords != nil {
						resultTotalRecords = int32(*results.TotalRecords)
					}

					if queryErr == nil {
						contextLogger.Debug("parsing result")

						if resultList, ok := results.Data.([]interface{}); ok {
							if len(resultList) == 0 {
								break
							}

							for _, v := range resultList {
								if resultRow, ok := v.(map[string]interface{}); ok {
									// Lock the metricList to prevent race conditions
									mu.Lock()
									for metricName, metric := range kusto.BuildPrometheusMetricList(queryConfig.Metric, queryConfig.MetricConfig, resultRow) {
										metricList.Add(metricName, metric...)
									}
									mu.Unlock()
								}
							}
						} else {
							break
						}

						contextLogger.Debug("metrics parsed")
					} else {
						contextLogger.Errorln(queryErr.Error())
						return
					}

					*RequestOptions.Skip += requestQueryTop
					if *RequestOptions.Skip >= resultTotalRecords {
						break
					}
				}

				elapsedTime := time.Since(startTime)
				contextLogger.With(zap.Int32("results", resultTotalRecords)).Debugf("fetched %v results", resultTotalRecords)
				prometheusQueryTime.With(prometheus.Labels{"module": moduleName, "metric": queryConfig.Metric}).Observe(elapsedTime.Seconds())
				prometheusQueryResults.With(prometheus.Labels{"module": moduleName, "metric": queryConfig.Metric}).Set(float64(resultTotalRecords))
			}(queryConfig) // Pass the queryConfig to the goroutine
		}

		// Wait for all queries to complete
		wg.Wait()

		// Store to cache if enabled
		if cacheTime.Seconds() > 0 {
			if cacheData, err := json.Marshal(metricList); err == nil {
				w.Header().Add("X-metrics-cached-until", time.Now().Add(cacheTime).Format(time.RFC3339))
				metricCache.Set(cacheKey, cacheData, cacheTime)
				probeLogger.Debugf("saved metric to cache for %s minutes", cacheTime.String())
			}
		}
	}

	// Build Prometheus metrics
	probeLogger.Debug("building prometheus metrics")
	for _, metricName := range metricList.GetMetricNames() {
		metricLabelNames := metricList.GetMetricLabelNames(metricName)

		gaugeVec := prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: metricName,
			Help: metricName,
		}, metricLabelNames)
		registry.MustRegister(gaugeVec)

		for _, metric := range metricList.GetMetricList(metricName) {
			for _, labelName := range metricLabelNames {
				if _, ok := metric.Labels[labelName]; !ok {
					metric.Labels[labelName] = ""
				}
			}

			if metric.Value != nil {
				gaugeVec.With(metric.Labels).Set(*metric.Value)
			}
		}
	}

	probeLogger.With(zap.String("duration", time.Since(requestTime).String())).Debug("finished request")
	h := promhttp.HandlerFor(registry, promhttp.HandlerOpts{})
	h.ServeHTTP(w, r)
}
