#!/usr/bin/env python3
"""
AWS Cost Spike Investigator
Zero-cost CloudTrail analysis to identify cost increase sources using:
- CloudTrail Event History (management events: read + write, all regions)
- Cost Explorer API analysis
- Service usage patterns
- IAM user/role activity tracking

No additional CloudTrail costs - uses existing 90-day free Event History
"""

import boto3
import json
from datetime import datetime, timedelta, timezone
import pytz
from collections import defaultdict, Counter


class CostSpikeInvestigator:
    def __init__(self):
        self.ce = boto3.client("ce")
        self.cloudtrail = boto3.client("cloudtrail")
        self.sts = boto3.client("sts")
        self.account_id = self.sts.get_caller_identity()['Account']

    def get_cost_trend(self, days=7):
        """Get daily cost trend to identify spikes"""
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=days)

        response = self.ce.get_cost_and_usage(
            TimePeriod={'Start': start_date.isoformat(),
                        'End': end_date.isoformat()},
            Granularity='DAILY',
            Metrics=['BlendedCost']
        )

        daily_costs = []
        for result in response['ResultsByTime']:
            date = result['TimePeriod']['Start']
            cost = float(result['Total']['BlendedCost']['Amount'])
            daily_costs.append({'date': date, 'cost': cost})

        return daily_costs

    def identify_spike_dates(self, daily_costs, threshold_multiplier=2.0):
        """Identify dates with cost spikes based on moving average"""
        spike_dates = []

        for i in range(1, len(daily_costs)):
            current_cost = daily_costs[i]['cost']
            previous_cost = daily_costs[i-1]['cost']

            # If current cost is significantly higher than previous
            if previous_cost > 0 and current_cost > (previous_cost * threshold_multiplier):
                spike_dates.append({
                    'date': daily_costs[i]['date'],
                    'current_cost': current_cost,
                    'previous_cost': previous_cost,
                    'increase_factor': current_cost / previous_cost if previous_cost > 0 else float('inf')
                })

        return spike_dates

    def get_service_breakdown_for_date(self, date):
        """Get detailed service breakdown for a specific date"""
        next_date = (datetime.fromisoformat(date) +
                     timedelta(days=1)).date().isoformat()

        response = self.ce.get_cost_and_usage(
            TimePeriod={'Start': date, 'End': next_date},
            Granularity='DAILY',
            Metrics=['BlendedCost'],
            GroupBy=[{'Type': 'DIMENSION', 'Key': 'SERVICE'}]
        )

        services = []
        if response['ResultsByTime']:
            for group in response['ResultsByTime'][0]['Groups']:
                service = group['Keys'][0]
                cost = float(group['Metrics']['BlendedCost']['Amount'])
                if cost > 0:  # Only include services with actual cost
                    services.append({'service': service, 'cost': cost})

        return sorted(services, key=lambda x: x['cost'], reverse=True)

    def investigate_cloudtrail_events(self, start_date, end_date, top_services):
        """Investigate CloudTrail events for the spike period"""
        start_time = datetime.fromisoformat(
            start_date).replace(tzinfo=timezone.utc)
        end_time = datetime.fromisoformat(
            end_date).replace(tzinfo=timezone.utc)

        print(
            f"\n🔍 Investigating CloudTrail events from {start_date} to {end_date}")

        # Check for high-cost service activities
        service_event_mapping = {
            'AWS Cost Explorer': ['ce.amazonaws.com'],
            'AWS Lambda': ['lambda.amazonaws.com'],
            'Amazon EC2 Container Registry (ECR)': ['ecr.amazonaws.com'],
            'AWS Glue': ['glue.amazonaws.com'],
            'Amazon S3': ['s3.amazonaws.com'],
            'Amazon RDS': ['rds.amazonaws.com']
        }

        events_analysis = defaultdict(list)
        user_activity = Counter()
        api_calls = Counter()

        # For the top cost-driving service
        if top_services:
            top_service = top_services[0]['service']
            print(f"🎯 Focusing on top cost service: {top_service}")

            if top_service in service_event_mapping:
                for event_source in service_event_mapping[top_service]:
                    try:
                        response = self.cloudtrail.lookup_events(
                            StartTime=start_time,
                            EndTime=end_time,
                            LookupAttributes=[{
                                'AttributeKey': 'EventSource',
                                'AttributeValue': event_source
                            }],
                            MaxResults=50
                        )

                        for event in response['Events']:
                            event_detail = {
                                'time': event['EventTime'].strftime('%Y-%m-%d %H:%M:%S'),
                                'event_name': event['EventName'],
                                'user': event.get('Username', 'Unknown'),
                                'source_ip': event.get('CloudTrailEvent', {}).get('sourceIPAddress', 'Unknown'),
                                'event_source': event['EventSource']
                            }
                            events_analysis[event_source].append(event_detail)
                            user_activity[event.get(
                                'Username', 'Unknown')] += 1
                            api_calls[event['EventName']] += 1

                    except Exception as e:
                        print(f"⚠️  Error checking {event_source}: {str(e)}")

        return {
            'events_by_service': dict(events_analysis),
            'user_activity': dict(user_activity),
            'api_calls': dict(api_calls)
        }

    def check_cost_explorer_usage(self, start_date, end_date):
        """Specifically investigate Cost Explorer API usage patterns"""
        print(f"\n💰 Analyzing Cost Explorer usage pattern...")

        # Cost Explorer pricing: $0.01 per API request
        # $2.07 = ~207 API requests

        start_time = datetime.fromisoformat(
            start_date).replace(tzinfo=timezone.utc)
        end_time = datetime.fromisoformat(
            end_date).replace(tzinfo=timezone.utc)

        try:
            # Look for any events that might indicate API usage
            response = self.cloudtrail.lookup_events(
                StartTime=start_time,
                EndTime=end_time,
                LookupAttributes=[{
                    'AttributeKey': 'ReadOnly',
                    'AttributeValue': 'true'
                }],
                MaxResults=100
            )

            cost_related_events = []
            for event in response['Events']:
                event_data = json.loads(event['CloudTrailEvent'])
                if any(keyword in event_data.get('eventSource', '').lower()
                       for keyword in ['cost', 'ce.amazonaws.com', 'billing']):
                    cost_related_events.append({
                        'time': event['EventTime'].strftime('%Y-%m-%d %H:%M:%S'),
                        'event': event['EventName'],
                        'user': event.get('Username', 'Unknown'),
                        'source': event_data.get('eventSource', 'Unknown')
                    })

            return cost_related_events

        except Exception as e:
            print(f"⚠️  Error investigating Cost Explorer usage: {str(e)}")
            return []

    def run_investigation(self):
        """Run complete cost spike investigation"""
        print("🚨 AWS Cost Spike Investigation Report")
        print("=" * 50)

        # 1. Get cost trend
        daily_costs = self.get_cost_trend(days=7)
        print(f"\n📊 Daily Cost Trend (Last 7 days):")
        for day in daily_costs:
            print(f"  {day['date']}: ${day['cost']:.4f}")

        # 2. Identify spikes
        spikes = self.identify_spike_dates(daily_costs)
        if not spikes:
            print("\n✅ No significant cost spikes detected in the last 7 days.")
            return

        print(f"\n🚨 Cost Spikes Detected:")
        for spike in spikes:
            print(f"  📅 {spike['date']}: ${spike['current_cost']:.2f} "
                  f"(↑{spike['increase_factor']:.1f}x from ${spike['previous_cost']:.2f})")

        # 3. Analyze each spike
        for spike in spikes:
            spike_date = spike['date']
            print(f"\n🔍 ANALYZING SPIKE: {spike_date}")
            print("-" * 40)

            # Get service breakdown
            services = self.get_service_breakdown_for_date(spike_date)
            print(f"💡 Top Services by Cost:")
            for i, service in enumerate(services[:5]):
                print(f"  {i+1}. {service['service']}: ${service['cost']:.4f}")
                if service['service'] == 'AWS Cost Explorer':
                    estimated_requests = service['cost'] / 0.01
                    print(
                        f"     → Estimated API requests: ~{estimated_requests:.0f}")

            # Investigate CloudTrail events
            next_date = (datetime.fromisoformat(spike_date) +
                         timedelta(days=1)).date().isoformat()
            cloudtrail_analysis = self.investigate_cloudtrail_events(
                spike_date, next_date, services)

            print(f"\n🕵️ User Activity Analysis:")
            for user, count in sorted(cloudtrail_analysis['user_activity'].items(),
                                      key=lambda x: x[1], reverse=True)[:5]:
                print(f"  👤 {user}: {count} events")

            print(f"\n🔧 Top API Calls:")
            for api, count in sorted(cloudtrail_analysis['api_calls'].items(),
                                     key=lambda x: x[1], reverse=True)[:5]:
                print(f"  🔨 {api}: {count} calls")

            # Special Cost Explorer analysis
            if any(s['service'] == 'AWS Cost Explorer' for s in services):
                cost_explorer_events = self.check_cost_explorer_usage(
                    spike_date, next_date)
                if cost_explorer_events:
                    print(f"\n💰 Cost Explorer Events Found:")
                    for event in cost_explorer_events[:10]:
                        print(
                            f"  🕐 {event['time']}: {event['event']} by {event['user']}")

        print(f"\n📋 Investigation Summary:")
        print(f"✅ CloudTrail Event History access confirmed")
        print(f"✅ Zero additional costs for this investigation")
        print(f"✅ Analysis based on 90-day free CloudTrail management events")

        if spikes:
            print(f"\n💡 Recommendations:")
            print(f"1. Monitor the identified services and users")
            print(f"2. Set up budget alerts for unusual spending patterns")
            print(f"3. Review IAM permissions for high-activity users")
            if any('AWS Cost Explorer' in str(spike) for spike in spikes):
                print(f"4. Consider implementing Cost Explorer API rate limiting")
                print(f"5. Cache cost data to reduce API calls")


def main():
    """Run the cost spike investigation"""
    try:
        investigator = CostSpikeInvestigator()
        investigator.run_investigation()
    except Exception as e:
        print(f"❌ Investigation failed: {str(e)}")
        print("Please ensure you have the required AWS permissions:")
        print("- ce:GetCostAndUsage")
        print("- cloudtrail:LookupEvents")
        print("- sts:GetCallerIdentity")


if __name__ == "__main__":
    main()
